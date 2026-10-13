# Phase 14 adapter handoff

The IDP path is disabled unless `LEGALDESK_IDP_ENABLED=true`. Existing upload,
clean promotion and RAG remain the source of truth when it is unset.

Handlers:

- `legaldesk.malware_scan_lambda.lambda_handler`: existing clean-scan boundary;
  when enabled, it creates the idempotent IDP intent after clean promotion.
- `legaldesk.idp_lambda.lambda_handler`: SQS IDP consumer with partial-batch
  failure handling and persisted-scope lookup.
- `legaldesk.idp_ocr_lambda.lambda_handler`: callback-only Textract completion
  consumer; it never polls.

Required enabled-mode configuration:

- `LEGALDESK_METADATA_TABLE_NAME`, `LEGALDESK_SOURCE_BUCKET`
- `LEGALDESK_IDP_QUEUE_URL`, `LEGALDESK_IDP_QUEUE_ARN`
- `LEGALDESK_IDP_MODEL_ID`, `LEGALDESK_IDP_PROMPT_VERSION`
- `LEGALDESK_IDP_OCR_SNS_TOPIC_ARN`, `LEGALDESK_IDP_OCR_ROLE_ARN`
- `LEGALDESK_IDP_OCR_SQS_SOURCE_ARN`
- review creation only, separately enabled by
  `LEGALDESK_IDP_REVIEW_ENABLED=true`: `LEGALDESK_IDP_GATEWAY_URL`,
  `LEGALDESK_IDP_TOKEN_ENDPOINT`, `LEGALDESK_IDP_M2M_CLIENT_ID`,
  `LEGALDESK_IDP_M2M_SECRET_PARAMETER_NAME` and
  `LEGALDESK_IDP_REVIEW_SCOPE` (`legaldesk-idp/review-create`). Worker and
  scheduled recovery use this fixed Gateway endpoint; no direct target call.
- interceptor and Review target additionally require `IDP_TABLE_NAME`,
  `LEGALDESK_SOURCE_BUCKET`, the matching dedicated client/scope,
  `LEGALDESK_IDP_REVIEW_TENANT_ID` and
  `LEGALDESK_IDP_REVIEW_MATTER_IDS` (one or two deployment-owned matter IDs).
  Missing scope configuration denies machine access rather than using a
  human identity or a test-only authorization exception.

Artifact bucket handoff (local deployment proposal, not deployed):

- IDP processing uses the existing source bucket for immutable normalized page
  artifacts. `phase-14-idp.yaml` sets
  `LEGALDESK_IDP_ARTIFACT_BUCKET` from its existing `SourceBucketName`; it
  does not create a bucket.
- The Phase 07 Review, Phase 08 MCP, and public-edge templates expose the
  additive `IDPArtifactBucketName` parameter with an empty default. Leave it
  empty when IDP is disabled. When enabling the artifact-read paths, the
  operator must pass the exact already-owned source/artifact bucket name (the
  same bucket used by `LEGALDESK_SOURCE_BUCKET`), after checking the reviewed
  change set.
- Runtime configuration treats an empty `LEGALDESK_IDP_ARTIFACT_BUCKET` as
  unset and falls back to `LEGALDESK_SOURCE_BUCKET`; an empty value must never
  become an S3 bucket name. No new bucket, KMS key, or fixed-charge component
  is part of this handoff.
- Approved read scopes are canonical
  `tenants/<tenant>/matters/<matter>/documents/*/original.pdf` and `.txt`,
  plus normalized IDP pages under
  `idp-artifacts/tenant=<tenant>/matter=<matter>/document=*/run=*/pages-*`.
  MCP/Review/public-edge readers do not read raw classifier or extractor
  outputs and do not receive object-list, write, or delete permissions.

Before any operator rollout, verify the exact bucket value, tenant/matter
allowlist, existing-table references, and Gateway schema change in a reviewed
change set. The parameter defaults preserve the current code-only and deployed
contracts when IDP remains disabled; this document is an adapter/IAM handoff,
not a production-readiness claim.

The server-controlled prompt artifacts are `prompts/idp-classifier.md` and
`prompts/idp-extractor.md`; packaged Lambda code resolves them from the
package prompt directory. Artifacts use the immutable prefix
`idp-artifacts/tenant=.../matter=.../document=.../run=.../` and are separate
from the existing `tenants/` RAG objects.

Required least-privilege resource classes for the reviewed IaC block are the
existing metadata table, one IDP SQS queue plus DLQ, one narrowly scoped OCR
SNS topic/subscription, and the existing malware Lambda's optional IDP send
permission. Runtime actions are scoped to the table, IDP artifact prefix,
queue/DLQ, OCR topic/role, `textract:StartDocumentTextDetection` and
`textract:GetDocumentTextDetection`, and the configured Bedrock model only.
Acquisition/processing does not invoke Gateway; the approved IDP review
creation path is the exception and uses the existing Gateway with the
dedicated Cognito M2M client/scope and purpose-bound grant. No human JWT or
always-on component is required.
