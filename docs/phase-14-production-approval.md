# Phase 14 production-promotion decision record

Status: **decision and provider evidence required**. Recorded from a read-only
inventory on 2026-09-28. This file is not an AWS authorization and contains no
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
  days. Source-bucket versioning is not enabled.
- GuardDuty Malware Protection is active only for the approved quarantine
  prefix. Reconciliation is enabled every 15 minutes.
- DynamoDB is on-demand with TTL and 35-day point-in-time recovery enabled.
  The observed table contained 42 items and about 15 KB.
- Twelve CloudWatch alarms were `OK`, but actions are disabled because no
  owned alarm topic is configured. The account-wide Budget is USD 25/month;
  a Budget alert is not a billing hard cap.
- Deployed monthly tenant ceilings are 20 uploads, 200 MiB uploads, 100 chats,
  20 ingestion starts, 20 Harness calls and 100 Gateway calls.
- The public composition revalidates retrieved documents against live scoped
  metadata and the canonical server-owned S3 object before resolver/writer use.
- Local release commit `830fe892aba17b218838479f2ee89d4cf05e4591`
  packages deterministically as Lambda artifact
  `2632928ac6e20e3ca23ae2e3e6a241e1aa456a50c6303b28e45d1ec46f8723be`
  and frontend artifact
  `07411bad1c0285d23d8c26f1e44841166ae12c87effb347f2903efa1ffe2d57d`.
  Its holdout preflight passed with zero AWS/network calls.

## Gates that still require evidence or an owner decision

1. **Semantic holdout:** runner `1.2.0` / adapter `2.1.0` needs one final frozen
   14-case provider run against the exact release commit and independent
   attestation. The most recent immutable run is valid failed evidence at
   13/14; it cannot be relabelled or overwritten.
2. **Deployment/rollback/recovery:** deploy the final immutable artifact,
   perform one bounded authenticated smoke, exercise rollback to the previous
   immutable artifact and forward recovery, exercise reconciliation with a
   synthetic stale fixture, and restore DynamoDB PITR into an isolated table
   before deleting that exact synthetic restore.
3. **Alert delivery:** create or name a project-owned SNS topic and confirm its
   subscription. Existing topics belong to another project and must not be
   reused. Until then alarm state is visible but not delivered.
4. **Transport:** the CloudFront default certificate fixes the viewer security
   policy to the legacy `TLSv1` policy. A strict production posture requires an
   approved custom hostname and a validated ACM certificate in `us-east-1`.
   Without them the release can only be described as a constrained beta with
   this residual risk, not strict production transport.
5. **Recovery/retention:** source-bucket versioning is disabled. Either enable
   versioning with an approved noncurrent-version lifecycle and restore test,
   or explicitly accept re-upload-only recovery for fictional/public source
   documents. Name the beta operator, application owner and data owner; approve
   review archival, residual metadata retention, legal-hold and export policy.

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
- source-bucket versioning/custom-domain resources only if separately selected
  and approved with their lifecycle/ownership.

All evidence must pin commit, artifact and template hashes. A failed holdout is
preserved and stops promotion; it is not retried under the same authorization.
