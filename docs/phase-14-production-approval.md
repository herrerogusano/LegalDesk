# Phase 14 production-promotion decision record

Status: **CONSTRAINED_PROD_BETA_DEPLOYED**. Updated on 2026-09-29
with the final holdout, smoke and deployment evidence. This file is not an AWS authorization and contains no
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
- All twelve CloudWatch alarms are `OK` and actions are enabled. The project
  SNS subscription is confirmed; the account-wide Budget is USD 25/month and
  is not a billing hard cap.
- Deployed monthly tenant ceilings are 20 uploads, 200 MiB uploads, 100 chats,
  20 ingestion starts, 20 Harness calls and 100 Gateway calls.
- The public composition revalidates retrieved documents against live scoped
  metadata and the canonical server-owned S3 object before resolver/writer use.
- Public-application release commit `0c8bb718e58811afff2085114dad7821cf573e3f`
  packages deterministically as Lambda artifact
  `99d074e793335f2867266b07ae091cb110a4747b8ad51e9180b031a2091f4f31`
  and frontend artifact
  `07411bad1c0285d23d8c26f1e44841166ae12c87effb347f2903efa1ffe2d57d`.
  The final4 holdout is metadata-only, passed 14/14, and has an independently
  approved attestation. Earlier failed reports remain immutable history.
- Document-security commit `65ca11ca44212f5e7991af23972ca8061a08ba17`
  is deployed with Lambda artifact SHA-256
  `27a5e8a2016f47e21f279a0f197632b1d32893c8316713dbd7099cf5414b5450`.
  It filters sidecars before Lambda and acknowledges a late event only when
  both metadata and the exact quarantine object are absent.

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
- The SNS subscription is confirmed and all twelve alarms are `OK`. The prior
  malware alarm was caused by two delayed synthetic fixture events (six Lambda
  attempts after retries), produced no promotion/indexing/DLQ message, and was
  closed after the bounded routing/idempotency fix was deployed.

## Historical blockers and final disposition

1. **Semantic holdout:** closed by final4 at 14/14 with independent approved
   attestation. All earlier unsuccessful runs and attestations remain preserved.
2. **Transport:** the CloudFront default certificate fixes the viewer security
   policy to the legacy `TLSv1` policy. A strict production posture requires an
   approved custom hostname and a validated ACM certificate in `us-east-1`.
   The owner explicitly accepts constrained promotion with this residual risk;
   the release does not claim strict production transport.
3. **Recovery/retention:** source-bucket versioning is intentionally not
   enabled. The fictional/public beta accepts re-upload-only recovery. A future
   version-aware design would need an explicit one-way CloudFormation decision,
   least-privilege version listing/deletion, lifecycle/delete-marker rules and
   an isolated restore/delete drill. For this solo project, the AWS/repository
   owner holds the beta-operator, application-owner and data-owner roles. The
   constrained beta keeps its documented archival, retention and export rules;
   real/client legal data and legal holds remain outside scope.
## Proposed bounded execution envelope

The following envelope was consumed for the recorded release exercise. It is
not reusable; another provider holdout requires a new explicit authorization:

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

## Final approval record — 2026-09-29

- Release app commit `0c8bb718e58811afff2085114dad7821cf573e3f`; artifact
  SHA-256 `99d074e793335f2867266b07ae091cb110a4747b8ad51e9180b031a2091f4f31`.
- Final4 holdout: 14/14, 27 calls, zero retries; report SHA-256
  `503b867a355c4b95ca69cb23f5746d8b1a58935f35f7e0204cafacfcead404ef`.
- Independent approved attestation v1.1.0 SHA-256
  `3d833232cabb1d290544009c31c7b9ecda0201264d9c331dc7d52cb799d48dc2`.
- Smoke `phase14-public-smoke-20260929-final4-02.json`: `PASS`; one chat,
  two citations, cross-matter `403`, audit `17`, logout `200`,
  `cleanupErrors=[]`; report SHA-256
  `1431503bd4a336bf552853af4cb8eb7b87a8cc488a44e59b7542e9624281d153`.
- Stack `UPDATE_COMPLETE`; only Lambda Code and dynamic API integration
  changed, with no replacement. S3 object version:
  `.VmAi0D4.baSxBpc61lKMBFkXobwoMyf`.
- All 12 alarms are `OK` and actions enabled. Solo-project governance roles
  are held by the AWS/repository owner; no personal name is invented.
- The default CloudFront TLS residual is explicitly accepted because no custom
  domain is owned. This is constrained-beta readiness, not strict-TLS or
  real-legal-data certification. Anonymous signup remains disabled.

Earlier failed reports and attestations remain immutable history, including the
11/14, 12/14 and 13/14 provider runs and their non-approved attestations. The
startup-failed smoke attempt `phase14-public-smoke-20260929-final4.json` is
also retained; `final4-02.json` is the passing smoke evidence.
