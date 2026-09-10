# Phase 01 deployment and teardown commands

These commands are intentionally documented but not executed without explicit
cost approval.

## Validate locally/service-side

```powershell
aws cloudformation validate-template `
  --region eu-west-1 `
  --template-body file://infra/cloudformation/phase-01-harness.yaml
```

## Deploy

Creates an IAM role plus a managed AgentCore Harness/underlying Runtime. Harness
and model invocations can incur usage charges.

```powershell
aws cloudformation deploy `
  --region eu-west-1 `
  --stack-name legaldesk-phase-01 `
  --template-file infra/cloudformation/phase-01-harness.yaml `
  --capabilities CAPABILITY_NAMED_IAM `
  --tags Project=LegalDesk Phase=01
```

## Invoke (maximum two smoke calls)

Obtain `HarnessArn` from the stack output, then run:

```powershell
python agent/scripts/invoke_harness.py `
  --harness-arn <HARNESS_ARN> `
  "Reply with exactly: LegalDesk Phase 01 ready"
```

Use a fresh UUID for the second call to demonstrate session isolation. Do not
put model, prompt, tool, or skill overrides under browser control.

## Teardown

```powershell
aws cloudformation delete-stack `
  --region eu-west-1 `
  --stack-name legaldesk-phase-01

aws cloudformation wait stack-delete-complete `
  --region eu-west-1 `
  --stack-name legaldesk-phase-01
```

CloudWatch log groups created by the runtime must be checked after teardown
because service-created log groups may outlive the stack.
