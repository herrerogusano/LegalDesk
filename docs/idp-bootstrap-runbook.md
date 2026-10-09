# Phase 14 IDP bootstrap and stack-update map

Status: operator plan and deployment checkpoint. This document does not itself
call AWS, create a change set, transfer a secret, invoke Gateway, or invoke
Bedrock/Textract. The disabled-first infrastructure changes are one reviewed
consolidated rollout; do not turn each compatible stack update or parameter
into a separate approval request. Enabling processing or machine review remains
a separate explicit gate.

## Inventory fixed by the read-only preflight

Use `eu-west-1` and account `344774635844`. These values came from the current
read-only inventory and must be rechecked immediately before a change set:

| Dependency | Value |
| --- | --- |
| Metadata table | `LegalDeskPhase02Documents-DocumentMetadataTable-1DANFLTX8RW7T` |
| Canonical source bucket | `legaldeskphase02documents-documentbucket-ojlu4kvhlied` |
| Immutable code-artifact bucket | `legaldeskphase08artifacts-legaldeskphase08artifact-4lbcgkaewrsw` |
| Cognito pool | `eu-west-1_tHvFPpktv` |
| Cognito domain | `legaldesk-phase08-344774635844.auth.eu-west-1.amazoncognito.com` |
| Gateway identifier | `legaldeskgatewayphase08-f17ddi2woq` (re-read the full ARN) |
| Public Edge application role | `arn:aws:iam::344774635844:role/LegalDeskPhase14PublicEdge-ApplicationExecutionRole-X3hgVtmlHWXr` |
| Public Edge application Lambda | `arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskPhase14PublicEdge-application` |
| Public Edge distribution | `arn:aws:cloudfront::344774635844:distribution/E7WKOUXC5SCOX` |
| Existing Public Edge API integration | `arn:aws:apigateway:eu-west-1::/apis/k7z9nuofg4/integrations/gs2d6w4` |
| Beta tenant | `tnt_phase14_20260928` |
| Beta matters | `mat_phase14_a_20260928`, `mat_phase14_b_20260928` |
| M2M SSM name | `/legaldesk/phase14/idp/m2m-client-secret` |
| M2M SSM ARN | `arn:aws:ssm:eu-west-1:344774635844:parameter/legaldesk/phase14/idp/m2m-client-secret` |
| IDP profile | `arn:aws:bedrock:eu-west-1:344774635844:inference-profile/eu.anthropic.claude-sonnet-4-6` |
| Ireland destination | `arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-sonnet-4-6` |
| Reviewed Lambda object | `phase-14/c136024881fc053e6a1e6a29097749a63214a2a9/legaldesk-lambda.zip` (SHA-256 `0757bfffd2d35028f548e4a232a71b1918de618689c2894f498a65fd86da02d2`) |

The IDP page-artifact bucket is the existing source bucket, under
`idp-artifacts/`; the immutable Lambda code-artifact bucket above is a
different bucket. No bucket or table is created by this bootstrap.

Re-read the exact Gateway ARN, queue outputs, Lambda code object version, and
all stack statuses. Do not substitute an ARN derived from a short identifier.

## Stack operations and exact parameter deltas

The candidate stack is `LegalDeskPhase14IDP`. Existing stack names are
`LegalDeskPhase14DocumentSecurity`, `LegalDeskPhase14Reconciliation`,
`LegalDeskPhase07ReviewTask`, `LegalDeskPhase08Gateway`, and
`LegalDeskPhase14PublicEdge`. If read-only inventory reports a different name,
stop and update the operator inventory; do not guess.

The temporary Public Edge service-role template is
`infra/cloudformation/phase-14-idp-bootstrap.yaml`. It creates only a
CloudFormation-trusted role with read access to the exact existing application
role/function/distribution and exact immutable Lambda object, plus
`iam:PutRolePolicy` on the existing application role and the exact Lambda
`iam:PassRole` condition. It has no `iam:*` wildcard, no Lambda invoke, and no
data-plane access. It has one metadata-only exception for
`logs:DescribeLogGroups`, constrained to `eu-west-1`; it cannot read log
events or write logs. The only API Gateway permission is `apigateway:GET` and
`apigateway:PATCH` on the exact existing integration
`arn:aws:apigateway:eu-west-1::/apis/k7z9nuofg4/integrations/gs2d6w4`, solely
for CloudFormation dependency re-evaluation. It does not create or address
any other API resource, and the reviewed update does not change that
integration's Lambda URI semantics.

For each existing stack, submit every parameter required by the checked-in
template. Preserve unchanged deployed values with `UsePreviousValue=true`.
The only code parameter changes are the immutable object bucket/key/version
from the approved Linux/Python 3.12 artifact; the production CD role is not
used for this bootstrap.

| Stack/template | First disabled update |
| --- | --- |
| `LegalDeskPhase14IDP` / `phase-14-idp.yaml` | **CREATE**. `EnableIDPProcessing=false`, `EnableIDPReviewDispatch=false`; existing metadata table/ARN, source bucket/ARN, code-artifact bucket, pool/domain, full Gateway ARN, exact profile/foundation ARNs, prompt version, and exact SSM name/ARN. Set both worker and OCR code key/version to the same immutable Lambda ZIP object when the artifact contains both handlers. |
| `LegalDeskPhase14DocumentSecurity` / `phase-14-document-security.yaml` | **UPDATE** with the new immutable malware Lambda code object. `EnableIDPProcessing=false`; queue URL may be empty and queue ARN may remain the reviewed disabled sentinel on this first update. `IDPModelId` and `IDPPromptVersion` remain empty while disabled. |
| `LegalDeskPhase14Reconciliation` / `phase-14-reconciliation.yaml` | **UPDATE** with the new immutable reconciliation code object. `EnableIDPProcessing=false`, `EnableIDPReviewDispatch=false`, `IDPGatewayUrl=""`, `IDPM2MClientId=""`, `ExistingUserPoolDomain=""`; keep the exact secret name/ARN and fixed scope. Preserve the existing schedule and `ReconciliationReservedConcurrency=0` configuration value; this does not mean the existing 15-minute rule is disabled or that a throttle is being applied. Do not create another timer. |
| `LegalDeskPhase07ReviewTask` / `phase-07-review-task.yaml` | **UPDATE** with the new immutable review Lambda code object. Keep `IDPM2MClientId`, tenant, matters, source bucket, and page-artifact bucket empty; keep `IDPReviewScope=legaldesk-idp/review-create`. This preserves the human-only target. |
| `LegalDeskPhase08Gateway` / `phase-08-gateway-mcp.yaml` | **UPDATE** with the new immutable MCP/interceptor code object(s). Keep `GatewayJwtIDPClientId`, IDP tenant/matters/source/artifact parameters empty; preserve all existing human/public client IDs and scopes. No new target or tool is added. |
| `LegalDeskPhase14PublicEdge` / `phase-14-public-edge.yaml` | **UPDATE** with the new immutable application Lambda code object. Keep `IDPArtifactBucketName=""` initially so no new artifact-read grant is activated. Preserve all existing edge, identity, quota, and NoEcho parameters with `UsePreviousValue=true`. |

The first IDP stack creates the queues, DLQs, Textract SNS topic/subscription,
roles, log groups, disabled functions, resource server, and dedicated
client. Event-source mappings are absent while `EnableIDPProcessing=false`;
the worker and OCR functions consume no reserved concurrency in that state.
The IDP client secret is not a CloudFormation output.

### Observed disabled-first checkpoint (2026-10-09)

The `LegalDeskPhase14IDP` stack reached `CREATE_COMPLETE`. Read-back showed
both IDP Lambda functions at reserved concurrency `0` with no event-source
mapping. The secret helper passed its exact Cognito-client checks and wrote
Standard `SecureString` parameter version 1 without exposing the value. The
temporary bootstrap role was created with the exact API-integration dependency;
its first Public Edge update rolled back safely to `UPDATE_ROLLBACK_COMPLETE`.
CloudFormation needed `logs:DescribeLogGroups` to resolve the existing
`LogsLogGroupArn` dependency, and the role did not yet have that regional
metadata permission. No log content or write permission was added. Document
Security and Reconciliation are `UPDATE_COMPLETE`, Review Task and Gateway
are `UPDATE_COMPLETE`, and the corrected Public Edge update remains pending.
These statuses do not authorize enablement.

After the first stack is created, re-read its queue URL/ARN, client ID, and
function ARNs. A second, still-disabled configuration update may replace the
sentinel queue values in Document Security/Reconciliation with those exact
outputs and set the server-selected profile/prompt version. It must not set
either enable switch true.

## Safe secret transfer

The only supported transfer is the narrow helper below. It never accepts a
secret argument, never prints a provider response, uses `SecureString` with
the Standard tier, and refuses to overwrite an existing parameter:

```powershell
python -B scripts/bootstrap_idp_secret.py `
  --region eu-west-1 `
  --user-pool-id eu-west-1_tHvFPpktv `
  --client-id <IDPMachineClient-output> `
  --execute `
  --approval I_UNDERSTAND_LEGALDESK_IDP_SECRET_BOOTSTRAP
```

The client ID is not secret. The helper obtains the secret from
`DescribeUserPoolClient` in memory, calls `PutParameter(Overwrite=false)`,
then drops the local reference. Do not place the secret in a parameter file,
environment variable, shell history, CloudFormation template, output, or
report. Verify only SSM parameter metadata with `DescribeParameters`; never
use `GetParameter(WithDecryption=true)` for a report.

## Reviewed change-set sequence

1. Run the offline tests and strict cfn-lint. Build or download the exact
   Linux/Python 3.12 release artifact retained by offline CI. Uploading that
   immutable ZIP to the existing code-artifact bucket is a separate approved
   operator action; record its S3 `VersionId` without logging document data or
   credentials.
2. Read-only inventory each existing stack with `describe-stacks`,
   `list-stack-resources`, and `get-template --template-stage Original`.
   Also inspect Lambda configuration names/safe values, SQS attributes,
   SNS subscription/topic policy metadata, and Gateway/Cognito metadata. Do
   not use `cloudformation deploy`, because it bypasses the reviewed
   change-set boundary.
3. Create the IDP **CREATE** change set with `CAPABILITY_IAM`, and the
   compatible disabled-first existing-stack updates as the same reviewed
   rollout. Inspect every resource list and parameter fingerprint. Confirm no
   event-source mapping, no enabled schedule, no CloudWatch alarm, no new
   bucket/table/KMS key, and no secret output. Execute the approved rollout in
   dependency order, observing each stack transition and stopping on any
   rollback or unexpected replacement; this observation boundary is not a new
   per-action approval requirement.
4. Use this execution order for the consolidated rollout: Document Security,
   Reconciliation, Review Task, Gateway, then Public Edge. A still-running
   update must finish or be rolled back before a dependent update is started.
5. Transfer the Cognito secret to SSM with the helper above as part of the
   disabled-first rollout. Keep IDP review dispatch disabled. A secret-transfer
   success is not authorization to enable the Gateway route.
6. When the backend transport and machine-review contract are approved,
   create a separate Review Task update with:
   `IDPM2MClientId=<new client ID>`,
   `IDPReviewTenantId=tnt_phase14_20260928`,
   `IDPReviewMatterIds=mat_phase14_a_20260928,mat_phase14_b_20260928`,
   `IDPSourceBucketName=legaldeskphase02documents-documentbucket-ojlu4kvhlied`,
   and `IDPArtifactBucketName=legaldeskphase02documents-documentbucket-ojlu4kvhlied`.
   Create a separate Gateway update with the same allowlist plus
   `GatewayJwtIDPClientId` and `GatewayJwtIDPScope=legaldesk-idp/review-create`.
   Review that the existing `create_review_task` target gains only the
   additive IDP contract; do not add a tool or direct Review Lambda path.
7. Only after offline composition, artifact verification, and cost approval:
   enable IDP processing in the IDP stack, then the clean-promotion and
   reconciliation producers in separate reviewed updates. Keep
   `EnableIDPReviewDispatch=false` until the machine route has its own
   acceptance. The existing budget alarm remains the cost guard; no new
   recurring alarm is part of this plan.

### Public-edge IAM boundary (normal implementation gate)

The deployed Public Edge stack currently records
`arn:aws:iam::344774635844:role/LegalDeskProductionCloudFormation` as its
CloudFormation service role. That role is intentionally code-only: it can
update the application Lambda code and API integration, but cannot patch the
application execution role's S3 policy. Therefore it must not be broadened
for IDP metadata pages, and the production CD workflow cannot perform the
Public Edge infrastructure update.

The disabled IDP stack and the producer/reconciliation/template updates may
be reviewed independently. If the deployed UI must read normalized IDP page
artifacts, use the checked-in temporary role only for the reviewed Public Edge
change set. This is normal implementation work within the approved phase; it
becomes a new approval gate only if the scope expands beyond the exact
existing resources/actions in that template or introduces fixed-cost
infrastructure. Do not substitute account-wide `AdministratorAccess`.

The exact role parameters are the read-only-inventoried application role ARN,
application function ARN, distribution ARN, existing code-artifact bucket ARN,
and the reviewed immutable object key. Pass the temporary role only on that
one update. Restore `LegalDeskProductionCloudFormation` in a separately
reviewed update and wait for a successful CloudFormation completion with the
original `RoleARN` binding confirmed by `DescribeStacks`; do not delete the
temporary role based on a no-op or an assumed role restoration. Only after
that proof may the temporary role be deleted. If the narrow role cannot be
used, leave `IDPArtifactBucketName` empty and record the UI artifact-read path
as not deployed rather than widening CD IAM.

Useful read-only examples (replace only the documented stack name):

```powershell
aws cloudformation describe-stacks --region eu-west-1 --stack-name <stack>
aws cloudformation list-stack-resources --region eu-west-1 --stack-name <stack>
aws cloudformation get-template --region eu-west-1 --stack-name <stack> --template-stage Original
aws lambda list-event-source-mappings --region eu-west-1 --function-name <function>
aws sqs get-queue-attributes --region eu-west-1 --queue-url <queue-url> --attribute-names All
aws sns get-topic-attributes --region eu-west-1 --topic-arn <topic-arn>
aws cognito-idp describe-user-pool --region eu-west-1 --user-pool-id eu-west-1_tHvFPpktv
```

## Permission and cost gates

The IDP worker/OCR roles are limited to the beta tenant's canonical
`original.pdf`/`original.txt`, `idp-artifacts/` page outputs, exact IDP job/OCR
state keys, exact SQS queues, the approved EU Sonnet profile and its six
verified destinations, Ireland-only Textract start/get exceptions, and the
exact Textract notification `PassRole`. Review-only SSM access and
`GATEWAY#IDP-INVOCATION#*` writes are conditional on review dispatch. There is
no Lambda invoke, Gateway invoke, human `GATEWAY#GRANT` write, DynamoDB Scan,
source DeleteObject, or blanket Bedrock permission.

The clean-promotion producer adds only conditional IDP job Put/Update and
exact work-queue SendMessage. Reconciliation adds only conditional IDP job
recovery/query, exact queue SendMessage, and conditional invocation/SSM
access. Review/MCP/interceptor reads are restricted to the two configured
matters and canonical/page artifacts when their IDP gates are non-empty.

Potential costs are existing-stack CloudFormation/Lambda updates, SQS/SNS
requests and retained logs/storage, SSM parameter storage, Cognito M2M token
requests, and—only after processing is enabled—Bedrock/Textract usage. No new
fixed-charge service, alarm topic, bucket, table, KMS key, Step Functions
state machine, or Gateway target is introduced. No deployment or paid call
has been made by this plan.
