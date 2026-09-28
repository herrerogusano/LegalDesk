# Phase 14 resume checkpoint

Recorded on 2026-09-28 after pausing the public-beta validation work.

## Continuation result — 2026-09-28

This checkpoint now includes the bounded evidence completed after the original
walkthrough. The release remains a constrained authenticated beta and is not
`READY_FOR_PROD`.

- Added a bounded public-browser smoke with a short-lived technical Cognito
  identity. Passwords remain process-memory-only; the runner performs no upload,
  ingestion, review creation or holdout and emits metadata-only reports.
- The first run demonstrated that three abandoned `PENDING_UPLOAD` rows blocked
  chat even though two documents were already `INDEXED`. The backend now blocks
  only when processing documents exist **and no indexed document is available**;
  retrieval still revalidates live indexed metadata before using evidence.
- A second live failure identified incomplete IAM for the configured
  cross-region inference profile. `InvokeModel` remains limited to the exact
  Sonnet 4.6 model family, with only the region wildcard required for the
  profile's documented routing set.
- Final authenticated Chrome-headless smoke: PASS. It verified two indexed
  documents, one bounded grounded chat, two citations, cross-matter `403`, audit
  retrieval and logout `200`. The synthetic user, membership, Memory events and
  subject-scoped state were removed; independent checks found no matching user,
  restored membership count and zero recent technical audit rows.
- Full local suite after the lifecycle fix and final review: 555 tests OK,
  1 skipped. The known Windows loopback `WinError 10053` appeared in one
  aggregate focused run; each affected test passed independently and the
  subsequent full suite passed.
- Deployment: `LegalDeskPhase14PublicEdge` is `UPDATE_COMPLETE`; the Lambda
  artifact is from commit `50dd36f`, and the IAM/template correction is commit
  `7194b00`. Metadata-only reports `01`–`06` preserve each bounded attempt.
- Independent readiness review initially kept the release closed because the
  only real holdout at that checkpoint was the immutable historical 11/14 run
  and the privacy/operations/cost approvals were incomplete. A zero-network
  preflight passed for release commit
  `19b86acc7a4f659c972cb960b0b41327674c4498` and Lambda SHA-256
  `eb36c4559176c433562f2273aaa9948661f65fa6f16695726eda2848f0c8c0be`,
  with both local safety canaries and a hard ceiling of 27 model calls.
- The separately authorized final-candidate holdout then accepted 12/14 with
  27 calls and zero retries. Its immutable report remains failed evidence; no
  attestation was created. Review traced both remaining failures to literal
  evaluation adapters rather than resolver/citation selection.
- Runner `1.1.0` and grounding adapter `2.0.0` now validate the conflict and
  directed relationship structurally, bind accepted cases to exact
  server-owned fixture citations, and make attestation verify complete
  per-case/counter/provenance consistency. The full 555-test suite passes. A
  new zero-network preflight passed for commit
  `623002eebf8ed556a8153ea6d3488229d617ee03`, the unchanged Lambda artifact
  SHA-256, and adapter implementation SHA-256
  `24a1cc4dbc4e0bdd26aaf47f03942e1ee91b4f25978a78a0da0c13a0d6cbfb59`.
- The next separately authorized execution used that exact candidate and
  accepted 13/14 with 27 calls and zero retries. `contradictory-deadlines`
  passed; `role-reversal` was still rejected as
  `ROLE_RELATIONSHIP_MISSING`. Its create-only metadata report is preserved as
  `phase14-holdout-20260928-adapter-v2.json`; no attestation was created.
- Because the privacy-preserving report contains no raw answers, the exact
  provider wording cannot be recovered. Runner `1.2.0` / adapter `2.1.0`
  therefore adds only bounded, adversarially tested relationship grammars
  (including double-object, cleft and actor-as-party forms) without changing
  prompts, fixture or application artifact. This is a local remediation, not
  passing provider evidence.
- A zero-network preflight passed for commit
  `f12c5c4b8bd5803db05c4737d55645e08659f8c7`, runner `1.2.0`, adapter `2.1.0`,
  adapter implementation SHA-256
  `629df88b14d7ea15589d46ce6c6d934c30d14a93ad7ce80b61a7e9d599bcc8e5`,
  and the unchanged Lambda artifact SHA-256. Both negative canaries passed;
  AWS/network calls were zero.

## Post-checkpoint evidence — 2026-09-28

- Final deployed HEAD is `fe53da68b1cb696de0741e94ff1ece99ecc3d710` with
  Lambda artifact SHA-256
  `2632928ac6e20e3ca23ae2e3e6a241e1aa456a50c6303b28e45d1ec46f8723be`.
- Public smoke report `phase14-public-smoke-20260928-08.json` is `PASS`:
  one `POST /api/chat`, cross-matter `403`, and zero cleanup errors.
- PITR restore into an isolated table contained 62 items and was deleted after
  verification. Rollback to the prior `50dd36f` artifact and forward recovery
  to the final `fe53da68...` artifact both reached `UPDATE_COMPLETE` and
  returned HTTP 200.
- Synthetic reconciliation passed with one invocation:
  `examined=6`, `changed=1`, `skipped=5`, `failed=0`, cleanup true.
- The SNS subscription is confirmed. Current alarm state is 11 `OK` and one
  `ALARM` (`MalwareScanErrorsAlarm`, two errors), so diagnosis remains a
  production-promotion blocker.
- The final holdout remains immutable failed evidence at 13/14 with
  `role-reversal` rejected as `ROLE_RELATIONSHIP_MISSING`; its attestation is
  `needs_follow_up`. The local adapter correction still requires a new
  authorized holdout. The custom domain is absent, so strict TLS remains
  blocked.

## Source state

- Branch: `phase/14-public-beta`
- Current release-candidate commit used by the revised holdout preflight:
  `f12c5c4b8bd5803db05c4737d55645e08659f8c7`.
- Integration target remains `developer`; no Phase 14 pull request or merge has
  been completed yet.
- Local-only `.agents/`, `skills-lock.json`, and `tmp/` are intentionally not
  part of the branch.

## Validation completed

- Full local suite: 555 tests passed, 1 skipped.
- Authenticated manual login and matter selection succeeded through CloudFront.
- A fictional text fixture was uploaded through the browser to the quarantine
  prefix, corroborated by GuardDuty, promoted to the canonical prefix, and
  indexed by the Knowledge Base.
- The completed ingestion job scanned two fictional test documents, indexed
  two, and reported zero failures.
- A real retrieval for `What is the inspection period?` returned the expected
  fictional passage: `The inspection period is four years.` with the correct
  tenant and matter metadata.

## Live fixes made during the walkthrough

1. Added `Content-Length` to the exact-origin S3 CORS allowlist for signed PUTs.
2. Forced regional virtual-hosted S3 presigned URLs so the upload destination
   matches the deployed CSP.
3. Prevented GuardDuty's quarantine verdict tag from being copied to the
   canonical object by using `TaggingDirective=REPLACE` with an empty tag set.
   This preserves least privilege and does not add `s3:PutObjectTagging`.
4. Added safe, allowlisted malware rejection reason codes without logging
   payloads, object keys, identifiers, or arbitrary exception text.
5. Replaced the internal `ing_<uuid>` value used as Bedrock `clientToken` with
   a deterministic SHA-256 token that satisfies the provider's character and
   length contract. The internal operation ID remains unchanged.
6. Removed three exact stale `STARTING` test-operation records after confirming
   that Bedrock had created no corresponding ingestion jobs. Documents and
   audit records were not removed.

## Deployed state

- `LegalDeskPhase14PublicEdge`: `UPDATE_COMPLETE`; the prior rollback artifact
  from commit `50dd36f` and forward recovery to final commit
  `fe53da68b1cb696de0741e94ff1ece99ecc3d710` were both verified with HTTP 200.
- `LegalDeskPhase14DocumentSecurity`: `UPDATE_COMPLETE`; malware artifact is
  from commit `ce86368bd43261fcaf1dc907b2204a6b1702f6ef`.
- The public application, malware scanner, GuardDuty plan, reconciliation,
  alarms, budget, Cognito, Gateway, Knowledge Base, and document stack remain
  deployed in `eu-west-1`.

## Remaining gates

1. The final holdout still requires a fresh explicit authorization for runner
   `1.2.0` / adapter `2.1.0`; only a new 14/14 report with independent
   approval can close that gate.
2. The custom CloudFront domain/strict TLS posture is still absent and remains
   a production transport blocker.
3. Complete the privacy/retention and operations owner sign-offs, then create
   the Phase 14 PR to `developer` after every gate passes. Promote `developer`
   to `prod` only through its separate release PR.
4. Update the shared session/vault again only after the remaining Phase 14 gates
   are complete; the earlier checkpoint has already been saved and synced.
