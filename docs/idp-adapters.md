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
- review creation only: `LEGALDESK_IDP_M2M_CLIENT_ID` and
  `LEGALDESK_IDP_REVIEW_SCOPE` (`legaldesk-idp/review-create`); these are
  consumed by the existing Gateway/interceptor/Review target, not by the
  upload or RAG path.

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
