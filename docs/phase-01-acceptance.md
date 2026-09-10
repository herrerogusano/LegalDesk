# Phase 01 acceptance record

## Local preparation

Validated on 2026-09-10 with Python 3.13.13, AWS CLI 2.35.20, and the
CloudFormation `ValidateTemplate` API in `eu-west-1`.

| Criterion | Evidence | Status |
|---|---|---|
| Minimal agent/config | CloudFormation Harness and Python invocation adapter | Ready locally |
| Reproducible deployment | `infra/cloudformation/phase-01-harness.yaml` | Ready locally |
| Different session IDs | UUID unit tests | Ready locally |
| No S3/KB/Gateway/Memory dependency | Template and policy regression test | Ready locally |
| IAM avoids unnecessary wildcard permissions | Scoped model/log ARNs and documented unavoidable wildcards | Ready locally |
| Harness vs Runtime documented | `docs/agentcore-harness-runtime.md` | Complete |
| Agent responds to real invocation | Requires deploy plus one paid-capable model call | Pending approval |

Phase 01 is not complete until the CloudFormation stack is deployed, its status
is checked, one or at most two real smoke invocations pass, and teardown
behavior is confirmed or the intentionally retained resource is documented.

## Verification completed

```text
python -m unittest discover -s tests -v
Ran 11 tests — OK

aws cloudformation validate-template ...
Valid; requires CAPABILITY_NAMED_IAM
```
