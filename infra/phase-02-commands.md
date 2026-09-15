# Phase 02 local/deployment commands

The template can be checked without creating resources:

```powershell
aws cloudformation validate-template --template-body file://infra/cloudformation/phase-02-document-pipeline.yaml --region eu-west-1
```

No AWS deployment is part of the local acceptance for Phase 02. If a later
approved deployment is needed, use a uniquely named stack and record its
outputs (bucket and table names) for the adapters. Before deleting the stack,
empty the S3 bucket (including all object versions if versioning was enabled by
an external policy); CloudFormation cannot delete a non-empty bucket:

```powershell
aws s3 rm s3://<DocumentBucketName> --recursive --region eu-west-1
aws cloudformation delete-stack --stack-name legaldesk-phase-02 --region eu-west-1
```

The template uses no `Retain` policy: after the bucket is empty, stack deletion
removes the bucket and DynamoDB table. Its 30-day S3 lifecycle rule is a
cost-conscious safety net, not a substitute for deliberate teardown. These
operations are intentionally not run by the Phase 02 worker.
