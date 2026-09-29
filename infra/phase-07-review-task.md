# Phase 07 — Review-task Lambda tool

## IaC boundary

`cloudformation/phase-07-review-task.yaml` creates the Lambda function, its
execution role, and one short-retention CloudWatch log group. It deliberately
does not create a second DynamoDB table: pass the existing Phase 02 table name
and ARN as parameters. The zip is also supplied as a versioned S3 artifact
(`ReviewTaskCodeBucket`, `ReviewTaskCodeKey`, and `ReviewTaskCodeVersion`) so a
deployment can be reproduced without embedding code in the stack.

The function uses Python 3.12, a 10-second timeout, and 256 MB memory. The
smoke account exposes only 10 total Lambda concurrency and requires 10 to
remain unreserved, so no per-function reserved cap is configured in this
deployment; add a cap after a quota increase and bounded-load observation.
The only data-plane permissions are `dynamodb:GetItem`, `dynamodb:PutItem`,
`dynamodb:Query`, and `dynamodb:UpdateItem` on the parameterized existing
table. `GetItem` supports idempotency and detail reads; `Query` is restricted
to the exact tenant/matter partition and a `REVIEW#` sort-key prefix, with a
metadata-only projection for list summaries; `PutItem` uses a server-owned key
and a condition that prevents replacement; `UpdateItem` requires the expected
current status and matter key. No Scan or second table is used.
The function receives the table name and schema version through environment
variables. The actor, tenant, matter, reason, and correlation ID must come
from the verified request context and validated tool input in the Lambda code;
they must not be trusted merely because a browser supplied them.

The log group is created by CloudFormation with 14-day retention so the role
does not need `logs:CreateLogGroup`. Its permissions are limited to creating
streams and writing events below that log group. Log events may contain only
allowlisted outcome metadata such as correlation ID, operation, task ID,
status, and latency. Do not log document bodies, prompts, PII, tokens, or
secrets. The log group and function are tagged `Project=LegalDesk` and
`Phase=07` and use delete policies for teardown.

No API, Gateway, MCP server, queue, notification channel, or extra database is
part of this phase. Phase 08 can expose this Lambda through AgentCore Gateway.
The Gateway target should pass the verified identity and server-built
`RequestContext` to the tool boundary, preserve correlation metadata, and
keep the tool contract narrow: create/list/get/update a bounded review task,
never legal advice. Gateway authorization and Lambda authorization remain
separate checks at their respective boundaries.

## Package and deploy checklist

Build the deployment zip from the Phase 07 Lambda source, including its
runtime dependencies if any. Upload it to a dedicated, private S3 bucket with
versioning enabled and record the resulting object version. The bucket and
upload role are outside this template and must follow the existing project
least-privilege policy. Do not put credentials or real legal documents in the
artifact. The handler in the zip must be importable as
`legaldesk.review_tasks.lambda_handler`.

Validate and deploy only after the artifact and Phase 02 table ARN have been
resolved:

```powershell
aws cloudformation validate-template `
  --template-body file://infra/cloudformation/phase-07-review-task.yaml `
  --region eu-west-1

aws cloudformation deploy `
  --stack-name LegalDeskPhase07ReviewTask `
  --template-file infra/cloudformation/phase-07-review-task.yaml `
  --parameter-overrides `
    ReviewTaskTableName=<existing-table-name> `
    ReviewTaskTableArn=<existing-table-arn> `
    ReviewTaskCodeBucket=<artifact-bucket> `
    ReviewTaskCodeKey=<artifact-key> `
    ReviewTaskCodeVersion=<artifact-version> `
  --region eu-west-1
```

With explicit approval, `LegalDeskPhase07ReviewTask` was deployed in
`eu-west-1` and retained as the Phase 08 review target. The synthetic Gateway
smoke created an OPEN task in the authorized matter, preserved idempotent
server ownership, and denied a cross-matter request before persistence. Local
tests still use fakes and never invoke AWS.

The deployment creates Lambda, IAM role, and CloudWatch log-group resources
and can incur AWS charges. Teardown is intentionally deferred while later
phases depend on the target; the versioned artifact is managed by the separate
Phase 08 artifact stack.

## Cost notes

Lambda requests, duration, and logs may be metered. DynamoDB reads/writes use
the existing table's billing mode and can add request cost. The 14-day log
retention and the account-level concurrency quota limit accidental growth but do not make
these services free. Keep smoke calls and test data small, monitor usage, and
reconfirm current AWS prices before deployment.
