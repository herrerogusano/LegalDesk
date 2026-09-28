# Phase 14 resume checkpoint

Recorded on 2026-09-28 after pausing the public-beta validation work.

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

1. Refresh the UI and verify the visible document status is `Indexed`, then
   exercise the grounded question, citation, review, and audit views.
2. Add or run a non-personal technical test identity for repeatable HTTP E2E.
   Never request or store the user's personal password.
3. Decide whether to remove the duplicate fictional upload produced by the
   repeated walkthrough attempts, then resync if it is removed.
4. Re-run the bounded real holdout against the final release artifact. The
   previous one-time holdout authorization has already been consumed, so this
   requires a fresh explicit authorization before any Bedrock inference calls.
5. Complete the final documentation/readiness review, create the Phase 14 PR to
   `developer`, merge after gates pass, and promote `developer` to `prod` only
   through its separate release PR.
6. Run the requested `save-session` workflow and update reusable RAG/MCP/AWS
   knowledge only after the final Phase 14 work is complete.

