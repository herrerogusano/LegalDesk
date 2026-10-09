# IDP deployment and validation evidence — 2026-10-09

Status: **IN PROGRESS**, not phase acceptance. Scope: owner-approved
`PHASE_14_IDP_PLAN.md`, ADR-020 machine identity, and continuation instruction
to finish the phase. No real legal documents or personal passwords are used.

## Release and inventory

- Account `344774635844`, region `eu-west-1`; existing dependency stacks were
  read back in completed states before changes.
- Offline CI `37951954927` passed the smoke-runner checkpoint. CI
  `37952679076` passed and retained the exact Linux/Python 3.12 release for
  `c136024881fc053e6a1e6a29097749a63214a2a9`.
- Application ZIP SHA-256:
  `0757bfffd2d35028f548e4a232a71b1918de618689c2894f498a65fd86da02d2`.
  Frontend ZIP SHA-256:
  `952c5fa5fd15029c93f4b8bb7d5285098991d36b808ace440204587b14f73502`.
- Both downloaded archives matched their manifest. Application artifact was
  uploaded create-only to existing versioned artifact storage at
  `phase-14/c136024881fc053e6a1e6a29097749a63214a2a9/legaldesk-lambda.zip`,
  version `mtJDZAPcKPRR_jR63azLQaz9Zz9TXSEv`, encrypted with SSE-S3.
- Existing monthly budget reports USD 25; its reported spend is delayed billing
  data, not evidence of zero account cost. All 12 existing operational alarms
  were `OK` with actions enabled. No Cost Explorer API was called.
- Approved EU Sonnet 4.6 profile is `ACTIVE` with six EU model destinations.
  Public regional Textract price list was rechecked: SKU `HV3WZRSJSH5TPZZ6`,
  first-million async text-detection pages USD 0.0015/page.

## Disabled-first infrastructure

Change set `idp-disabled-c136024-20261009` for new stack
`LegalDeskPhase14IDP` was reviewed before execution: exactly 17 additions,
consisting of four SQS queues, Textract SNS completion channel/policies/
subscription, two Lambda functions/log groups, three execution/notification
roles, and the dedicated Cognito resource server/client. No new data table,
bucket, KMS key, fixed-capacity service or enabled event-source mapping.
`EnableIDPProcessing=false` and `EnableIDPReviewDispatch=false`.
The stack reached `CREATE_COMPLETE`. Read-back confirmed both IDP Lambda
functions have reserved concurrency `0` and no event-source mapping. The
disabled-first rollout is therefore deployed without an active worker or OCR
consumer.

The same consolidated rollout read back `UPDATE_COMPLETE` for
`LegalDeskPhase14DocumentSecurity`, `LegalDeskPhase14Reconciliation`, and
`LegalDeskPhase07ReviewTask`. `LegalDeskPhase08Gateway` is `UPDATE_COMPLETE`.
The first `LegalDeskPhase14PublicEdge` update rolled back safely to
`UPDATE_ROLLBACK_COMPLETE`: CloudFormation needed
`logs:DescribeLogGroups` to resolve the existing `LogsLogGroupArn` dependency,
but the temporary role did not yet have that regional metadata permission.
No log content or write permission was involved. A corrected Public Edge
change set remains pending. These are control-plane statuses, not evidence
that processing or machine review is enabled.

The exact Cognito client was validated before the secret transfer. The helper
passed and wrote Standard `SecureString` parameter version 1 without logging
or outputting the secret. Review dispatch remains disabled.

## Public-edge service-role constraint

The existing stack uses `LegalDeskProductionCloudFormation`, intentionally
code-only. IDP additionally needs an exact artifact-read policy and environment
configuration. That CD role remains unchanged. A temporary operator-managed
CloudFormation role was created, scoped only to the inventoried existing
application role/function, immutable code object, read-only distribution
metadata, and the exact existing API integration GET/PATCH dependency required
for URI re-evaluation. The first Public Edge update using that role rolled back
safely because CloudFormation also required regional `logs:DescribeLogGroups`
metadata access to resolve `LogsLogGroupArn`; no log content or write access
was granted. The corrected Public Edge update remains pending, and the reviewed
integration URI has no intended semantic change. The role must be
removed only after the stack is demonstrably rebound to the original code-only
role. No account-wide IAM grant is authorized or needed.

## Canary result (not acceptance)

The immutable evaluation export
`evals/results/idp-canary-contract-20261009-a.json` records the first real
`contract-01-en-digital-monthend` attempt. It is `FAILED` with
`EVALUATION_FAILED` and `PAID_OUTCOME_UNKNOWN`; provenance is `UNKNOWN` with
two Bedrock calls recorded and provider usage unknown. This is neither a
successful inference result nor evidence that no provider call occurred. It is
retained as an honest failed/unknown result and must not be counted as a pass,
as real-model acceptance, or as zero cost.

## Subsequent diagnostic and deployment checkpoint

- Review Task and Gateway updates reached `UPDATE_COMPLETE`. Metadata MCP
  target is `READY` with `listingMode=DYNAMIC`; explicit synchronization was
  rejected as unsupported for this mode, so no static-cache refresh is claimed.
- Public Edge dependency-read correction was reviewed and executed using
  change set `idp-public-edge-dependencies-c136024`; the stack reached
  `UPDATE_COMPLETE`. Its service-role binding still requires restoration.
- Four immutable contract reports (`a`, `diagnostic-b`, `schema-fixed-c`,
  `nullable-d`) preserve failed/unknown attempts rather than overwriting them.
  The diagnostic identifies extractor provider validation. Isolated synthetic
  schema checks revealed 61 optional parameters exceeding the provider limit
  of 24, followed by a grammar-too-large error after the first simplification.
- The compact internal array schema is decoded into the unchanged persisted
  mapping, with duplicate/name/coverage and existing typed/evidence validation.
  Extractor prompt is now `1.0.2`. An isolated synthetic schema check succeeded
  with `end_turn`, 1,517 input and 516 output tokens; raw response was not saved.
  This proves provider grammar acceptance, not extraction accuracy or AWS E2E.
- Supervisor full offline suite completed: 806 tests, one host symlink skip.
  A fresh Linux release/CI is required for the provider and prompt correction.

## Validation still outstanding

Machine Gateway acceptance, processing enablement, real-model/OCR acceptance,
real AWS E2E, and cleanup verification remain open. Failed canaries are
not accepted evidence. No Textract
processing has been executed at this checkpoint. Storage/deployment/control-
plane activity is not asserted free. A successful offline gate or
`CREATE_COMPLETE`/`UPDATE_COMPLETE` status is not phase acceptance.

## Rollback references captured

- The immutable application artifact remains available at S3 version
  `mtJDZAPcKPRR_jR63azLQaz9Zz9TXSEv` with SHA-256
  `0757bfffd2d35028f548e4a232a71b1918de618689c2894f498a65fd86da02d2`.
- The existing Public Edge service-role rollback target is
  `arn:aws:iam::344774635844:role/LegalDeskProductionCloudFormation`; restore
  it through a reviewed successful stack update and confirm the original
  `RoleARN` before deleting the temporary bootstrap role.
- For the new IDP stack and disabled existing-stack updates, use the reviewed
  CloudFormation change-set/template history and the pre-update immutable code
  versions captured by the operator; no unrecorded rollback identifier is
  inferred here.
