# Phase 14 production-promotion decision record

Status: **promotion remains blocked**. Updated on 2026-09-28 with the bounded
deployment and operations evidence completed today. This file is not an AWS authorization and contains no
secret, token, document body, prompt, answer or credential.

## Verified deployed baseline

- Account `344774635844`, region `eu-west-1`.
- Public CloudFront hostname `d3nxeyrpa3juwl.cloudfront.net`; authenticated
  public browser smoke passed previously.
- Phase 14 public edge, document-security, reconciliation and operations
  stacks are healthy. The related Phase 02/03/06/07/08/10/11 resources are
  also healthy.
- Cognito is admin-create-only. OAuth authorization-code flow uses the exact
  CloudFront callback/logout URLs; the public client has no secret and token
  revocation is enabled.
- S3 public access is fully blocked, SSE-S3 is enabled, exact CORS is deployed,
  quarantine expires after one day and canonical `tenants/` objects after 30
  days. The deployed source bucket is unversioned; fictional/public beta
  recovery is re-upload-only. Version-aware recovery is not claimed.
- GuardDuty Malware Protection is active only for the approved quarantine
  prefix. Reconciliation is enabled every 15 minutes.
- DynamoDB is on-demand with TTL and 35-day point-in-time recovery enabled.
  The observed table contained 42 items and about 15 KB.
- Eleven CloudWatch alarms were `OK`; `MalwareScanErrorsAlarm` is currently in
  `ALARM` for two errors and remains under diagnosis/block. The project SNS
  subscription is confirmed; the account-wide Budget is USD 25/month and is
  not a billing hard cap.
- Deployed monthly tenant ceilings are 20 uploads, 200 MiB uploads, 100 chats,
  20 ingestion starts, 20 Harness calls and 100 Gateway calls.
- The public composition revalidates retrieved documents against live scoped
  metadata and the canonical server-owned S3 object before resolver/writer use.
- Final deployed release commit `fe53da68b1cb696de0741e94ff1ece99ecc3d710`
  packages deterministically as Lambda artifact
  `2632928ac6e20e3ca23ae2e3e6a241e1aa456a50c6303b28e45d1ec46f8723be`
  and frontend artifact
  `07411bad1c0285d23d8c26f1e44841166ae12c87effb347f2903efa1ffe2d57d`.
  The final holdout report is metadata-only and remains failed evidence at
  13/14; its report SHA-256 is recorded by the attestation file.

## Completed bounded evidence — 2026-09-28

- Final public smoke report `phase14-public-smoke-20260928-08.json`: `PASS`;
  exactly one `POST /api/chat`, cross-matter status `403`, and
  `cleanupErrors=[]` (report SHA-256
  `1431503bd4a336bf552853af4cb8eb7b87a8cc488a44e59b7542e9624281d153`).
- DynamoDB PITR was restored into an isolated table with 62 items; that exact
  synthetic restore was then deleted.
- Rollback to the prior artifact from commit `50dd36f` and forward recovery to
  `fe53da68b1cb696de0741e94ff1ece99ecc3d710` both reached `UPDATE_COMPLETE`
  and returned HTTP 200 in the bounded check.
- Synthetic reconciliation passed with one invocation:
  `examined=6`, `changed=1`, `skipped=5`, `failed=0`, cleanup verified.
- The SNS subscription is confirmed. Current alarm state is 11 `OK` and one
  `ALARM` (`MalwareScanErrorsAlarm`, two errors); the alarm finding remains a
  production-promotion blocker pending diagnosis.

## Gates that still require evidence or an owner decision

1. **Semantic holdout:** runner `1.2.0` / adapter `2.1.0` still needs a newly
   authorized 14-case provider run against the final release and independent
   attestation. The immutable final report remains `13/14`,
   `needs_follow_up`, with `role-reversal` rejected as
   `ROLE_RELATIONSHIP_MISSING`; it cannot be relabelled or overwritten. The
   local adapter correction requires that fresh authorization.
2. **Transport:** the CloudFront default certificate fixes the viewer security
   policy to the legacy `TLSv1` policy. A strict production posture requires an
   approved custom hostname and a validated ACM certificate in `us-east-1`.
   Without them the release can only be described as a constrained beta with
   this residual risk, not strict production transport.
3. **Recovery/retention:** source-bucket versioning is intentionally not
   enabled. The fictional/public beta accepts re-upload-only recovery. A future
   version-aware design would need an explicit one-way CloudFormation decision,
   least-privilege version listing/deletion, lifecycle/delete-marker rules and
   an isolated restore/delete drill. Name the beta operator, application owner
   and data owner; approve review archival, residual metadata retention,
   legal-hold and export policy.
4. **Alarm health:** `MalwareScanErrorsAlarm` is currently `ALARM` for two
   errors while the other eleven alarms are `OK`. Diagnose and close this
   finding before any production promotion; the confirmed SNS subscription
   does not turn an active alarm into a pass.

## Proposed bounded execution envelope

The next provider operation should be approved as one indivisible release
exercise for this exact account and region:

- maximum 27 real-model calls for the frozen holdout, zero retries, estimated
  below USD 2 with a USD 5 residual one-time envelope (not a guaranteed billing
  cap and an in-flight request may cross a threshold);
- one final artifact upload/deployment, one synthetic browser smoke, one
  synthetic reconciliation fixture, one isolated PITR restore/delete, one
  rollback and one forward recovery;
- at most one Knowledge Base synchronization only if the synthetic document
  path requires it;
- no real/client legal data, no anonymous signup, no broad IAM, no new region
  and no unrelated infrastructure;
- a dedicated alarm topic/subscription only if explicitly approved;
- custom-domain resources only if separately selected and approved with their
  lifecycle/ownership. Source-bucket versioning is deferred and is not part of
  this release envelope.

All evidence must pin commit, artifact and template hashes. A failed holdout is
preserved and stops promotion; it is not retried under the same authorization.
