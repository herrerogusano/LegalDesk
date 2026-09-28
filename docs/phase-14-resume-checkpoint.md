# Phase 14 resume checkpoint

Recorded on 2026-09-28 after pausing the public-beta validation work.

## Continuation result — 2026-09-28

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
- Full local suite after the lifecycle fix and final review: 552 tests OK, 1 skipped. The known
  Windows loopback `WinError 10053` appeared in one aggregate focused run; each
  affected test passed independently and the subsequent full suite passed.
- Deployment: `LegalDeskPhase14PublicEdge` is `UPDATE_COMPLETE`; the Lambda
  artifact is from commit `50dd36f`, and the IAM/template correction is commit
  `7194b00`. Metadata-only reports `01`–`06` preserve each bounded attempt.

## Source state

- Branch: `phase/14-public-beta`
- Last validated code commit before this checkpoint: `5cb5c8ce26a1a1615bdabedb51dc26c88211149b`
- Integration target remains `developer`; no Phase 14 pull request or merge has
  been completed yet.
- Local-only `.agents/`, `skills-lock.json`, and `tmp/` are intentionally not
  part of the branch.

## Validation completed

- Full local suite: 544 tests passed, 1 skipped.
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

- `LegalDeskPhase14PublicEdge`: `UPDATE_COMPLETE`; application artifact is from
  commit `5cb5c8ce26a1a1615bdabedb51dc26c88211149b`.
- `LegalDeskPhase14DocumentSecurity`: `UPDATE_COMPLETE`; malware artifact is
  from commit `ce86368bd43261fcaf1dc907b2204a6b1702f6ef`.
- The public application, malware scanner, GuardDuty plan, reconciliation,
  alarms, budget, Cognito, Gateway, Knowledge Base, and document stack remain
  deployed in `eu-west-1`.

## Remaining gates

1. The authenticated technical journey now covers document status, grounded
   question, citation, cross-matter denial, audit and logout. Review creation
   retains its earlier manual/live evidence and was intentionally not repeated
   by the cleanup-oriented technical runner.
2. Decide whether to remove the duplicate fictional upload produced by the
   repeated walkthrough attempts, then resync if it is removed.
3. Re-run the bounded real holdout against the final release artifact. The
   previous one-time holdout authorization has already been consumed, so this
   requires a fresh explicit authorization before any Bedrock inference calls.
4. Complete the final documentation/readiness review, create the Phase 14 PR to
   `developer`, merge after gates pass, and promote `developer` to `prod` only
   through its separate release PR.
5. Update the shared session/vault again only after the remaining Phase 14 gates
   are complete; the earlier checkpoint has already been saved and synced.
