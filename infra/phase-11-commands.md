# Phase 11 deployment and teardown

These commands use only existing Phase 07/08 log groups. The stack creates
metric filters and can incur CloudWatch Logs/custom-metric charges; review the
change set before execution. It does not enable AgentCore payload logs,
Transaction Search, or a new log group.

## Validate and deploy

```powershell
$region = "eu-west-1"
$stack = "LegalDeskPhase11Observability"
$template = "infra/cloudformation/phase-11-observability.yaml"

aws cloudformation validate-template `
  --region $region `
  --template-body file://$template

aws cloudformation deploy `
  --region $region `
  --stack-name $stack `
  --template-file $template `
  --parameter-overrides `
    GatewayLogGroupName=/aws/lambda/LegalDeskGatewayRequestInterceptorPhase08 `
    MetadataMcpLogGroupName=/aws/lambda/LegalDeskMetadataMcpPhase08 `
    ReviewTaskLogGroupName=/aws/lambda/LegalDeskReviewTaskPhase07 `
  --tags Project=LegalDesk Phase=11 `
  --no-fail-on-empty-changeset
```

For a reviewed change set instead of the convenience deploy:

```powershell
aws cloudformation create-change-set `
  --region eu-west-1 `
  --stack-name LegalDeskPhase11Observability `
  --change-set-name phase11-review `
  --change-set-type CREATE `
  --template-body file://infra/cloudformation/phase-11-observability.yaml `
  --tags Key=Project,Value=LegalDesk Key=Phase,Value=11

aws cloudformation describe-change-set `
  --region eu-west-1 `
  --stack-name LegalDeskPhase11Observability `
  --change-set-name phase11-review
```

The first deployment must show only `AWS::Logs::MetricFilter` additions. Any
log-group replacement, deletion, or unexpected resource is a stop condition.

## Read-only inventory / dry-run teardown

```powershell
aws cloudformation list-stack-resources `
  --region eu-west-1 `
  --stack-name LegalDeskPhase11Observability

aws logs describe-metric-filters `
  --region eu-west-1 `
  --log-group-name /aws/lambda/LegalDeskGatewayRequestInterceptorPhase08
```

The inventory must be reviewed before deletion. The Phase 11 stack owns only
metric filters, so deleting it does not delete existing log groups or any
Phase 01/07/08/09 resources.

## Teardown

```powershell
aws cloudformation delete-stack `
  --region eu-west-1 `
  --stack-name LegalDeskPhase11Observability
aws cloudformation wait stack-delete-complete `
  --region eu-west-1 `
  --stack-name LegalDeskPhase11Observability
```

Do not delete the existing Lambda or AgentCore log groups as part of this
stack teardown. Runtime-created log groups, retained artifacts, Gateway,
Harness, Memory, Cognito, and the existing metadata table require their own
dependency review.
