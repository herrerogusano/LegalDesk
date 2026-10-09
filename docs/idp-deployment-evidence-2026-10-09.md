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
No log content or write permission was involved. The corrected Public Edge
update then reached `UPDATE_COMPLETE`; the temporary role remains bound and
still requires restoration to the original code-only role. These are
control-plane statuses, not evidence that processing or machine review is
enabled.

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
was granted. The corrected Public Edge update reached `UPDATE_COMPLETE`, and
the reviewed integration URI has no intended semantic change. The role must be
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
  `UPDATE_COMPLETE`. Its temporary service-role binding still requires
  restoration to the original code-only role.
- Four immutable contract reports (`a`, `diagnostic-b`, `schema-fixed-c`,
  `nullable-d`) preserve failed/unknown attempts rather than overwriting them.
  The diagnostic identifies extractor provider validation. Isolated synthetic
  schema checks revealed 61 optional parameters exceeding the provider limit
  of 24, followed by a grammar-too-large error after the first simplification.
- The corrected compact-prompt canary is preserved at
  `evals/results/idp-canary-contract-20261009-compact-e.json`: prompt `1.0.2`,
  `COMPLETE`, two provider calls, 2,272 input tokens, 789 output tokens, and
  field-level `REVIEW_REQUIRED`. This is provider completion evidence, not
  phase acceptance.
- The immutable digital corpus report
  `evals/results/idp-digital-corpus-20261009-f.json` covers nine documents and
  18 provider calls (16,519 input and 4,817 output tokens) with expected
  document classes. Its historical `COMPLETED` status for review fields is an
  export-enum erratum; the report is preserved and those fields are interpreted
  under the corrected review-required contract.
- The compact internal array schema is decoded into the unchanged persisted
  mapping, with duplicate/name/coverage and existing typed/evidence validation.
  Extractor prompt is now `1.0.2`. An isolated synthetic schema check succeeded
  with `end_turn`, 1,517 input and 516 output tokens; raw response was not saved.
  This proves provider grammar acceptance, not extraction accuracy or AWS E2E.
  The observed provider boundary is consistent with AWS Bedrock's documented
  structured-output schema validation and first-time grammar compilation; see
  [AWS structured outputs](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
  and [Anthropic Claude on Amazon Bedrock](https://docs.anthropic.com/en/api/claude-on-amazon-bedrock).
- CI run `37955864825` failed stale-runtime fake contract tests and remains
  retained as diagnostic evidence. Corrected CI run `37956195348` passed.
- The OCR collector is still running against eight synthetic PDFs as whole
  PDFs (11 pages, bounded at 22 pages); this is not production Textract
  continuation evidence. New worker duplicate-proof telemetry is pending the
  next CI release.

## Validation still outstanding

Machine Gateway acceptance, processing enablement, real-model/OCR acceptance,
real AWS E2E, and cleanup verification remain open. Failed canaries and
review-required canaries are not phase acceptance. The OCR collector is not
production continuation proof, and worker duplicate telemetry is pending. No
production Textract continuation has been accepted at this checkpoint.
Storage/deployment/control-plane activity is not asserted free. A successful
offline gate or `CREATE_COMPLETE`/`UPDATE_COMPLETE` status is not phase
acceptance.

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
