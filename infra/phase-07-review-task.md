# Phase 07 — Review-task Lambda tool

## IaC boundary

`cloudformation/phase-07-review-task.yaml` creates the Lambda function, its
execution role, and one short-retention CloudWatch log group. It deliberately
does not create a second DynamoDB table: pass the existing Phase 02 table name
and ARN as parameters. The zip is also supplied as a versioned S3 artifact
(`ReviewTaskCodeBucket`, `ReviewTaskCodeKey`, and `ReviewTaskCodeVersion`) so a
deployment can be reproduced without embedding code in the stack.

The function uses Python 3.12, a 10-second timeout, 256 MB memory, and a
reserved concurrency of 5. These are starting limits for a small MVP tool and
should be adjusted only after observing bounded load. The only data-plane
permissions are `dynamodb:GetItem` and `dynamodb:PutItem` on the parameterized
existing table. `GetItem` supports idempotency lookup; `PutItem` must use a
server-owned key and a condition that prevents replacing an existing task.
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
keep the tool contract narrow: create a review task and return its ID/status,
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

The deploy creates Lambda, IAM role, and CloudWatch log-group resources and
can incur AWS charges. Phase 07 local tests and IaC validation are sufficient
for this target-ready deliverable; do not deploy or invoke it as part of local
tests. If a smoke is authorized later, use synthetic context and one
authorized create, one repeated idempotency request, and one cross-matter
request. Verify the cross-matter request is denied before `PutItem`, then
delete the stack and wait for deletion:

```powershell
aws cloudformation delete-stack --stack-name LegalDeskPhase07ReviewTask --region eu-west-1
aws cloudformation wait stack-delete-complete --stack-name LegalDeskPhase07ReviewTask --region eu-west-1
```

Review the stack resource list after deletion and confirm no function, role,
log group, or other stack resource remains. The separately managed artifact
bucket/object is not deleted by this stack and needs its own approved cleanup
procedure.

## Cost notes

Lambda requests, duration, and logs may be metered. DynamoDB reads/writes use
the existing table's billing mode and can add request cost. The 14-day log
retention and reserved concurrency cap limit accidental growth but do not make
these services free. Keep smoke calls and test data small, monitor usage, and
reconfirm current AWS prices before deployment.
