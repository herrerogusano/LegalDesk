# Release preparation — 2026-10-01

Status: **published and verified**.

This record covers the approved workspace refinement release: review-motion and
workspace UI changes, expired-session logout recovery, diagnostics, catalog and
error metadata updates, plus the associated tests and offline CI. It does not
change RAG, prompts, retrieval, ingestion, Guardrails, model configuration,
IAM policy, data-plane resources or user data. The initial preparation was
read-only; the reviewed artifact uploads, change-set execution and CloudFront
invalidation are recorded below.

## Inventory and deployment boundary

- Branch: `codex/workspace-refinement`; target branch: `developer`.
- AWS region: `eu-west-1`.
- Existing stack: `LegalDeskPhase14PublicEdge`, `UPDATE_COMPLETE`.
- Read-only inventory confirmed the existing API, six routes (including
  `GET /logout` and `POST /logout`), CloudFront distribution, Lambda, frontend
  bucket, logs and execution role. No new resource was created.
- The current public baseline returned `200` for the root/static legacy assets,
  `302` for `GET /logout`, and `403` for unauthenticated `/api/me`. The new
  `/diagnostics.js` path was not present in that baseline (`403`); the published
  result below verifies the new asset.

## Reproducible artifacts

The only packager used was `scripts/package_release.py`. It ran offline against
the previously prepared Linux dependency payload `tmp/package-deps-50dd36f`;
there were no dependency upgrades, downloads, pip calls or AWS calls. The
post-fix rebuild after the symlink-root guard produced the same artifact and
manifest digests as the first two builds.

Output directory: `dist/phase14-20261001-c/` (ignored build output).

| Artifact | Size | SHA-256 |
|---|---:|---|
| `legaldesk-lambda.zip` | 21,613,493 bytes | `267840f0bbb7fdfc644c8dc41bccadcb93c68959738df8a085719cdec9ad8200` |
| `legaldesk-frontend.zip` | 33,487 bytes | `fc910c64458d3b7dac25410df7b7b38109bf95feb441e01a567bfb82fa19e0ba` |
| `legaldesk-release-manifest.json` | 496,164 bytes | `84da916fa0a96ffb13fac748fab896f8e810b949d25630fb9177d9533bebe741` |

The frontend artifact contains exactly `index.html`, `styles.css`, `app.js`,
`citations.js` and `diagnostics.js`. The Lambda manifest contains 2,414 files.
The dependency versions match the pinned constraints: boto3/botocore 1.43.97,
cffi 2.1.1, cryptography 50.0.1, jmespath 1.1.0, PyJWT 2.14.0,
pycparser 3.0, s3transfer 0.19.2 and urllib3 2.8.0 (with the existing
transitive `python-dateutil` and `six` payload).

The 2,377 dependency-input file hashes match the prior deployed `dfa85e4`
manifest exactly; only the application source payload and the new five-file
frontend payload differ.

## Gates run

- Release packaging tests: 4 passed, 1 platform-dependent skip.
- Targeted API/frontend/HTTP security tests: 29 passed on the rerun. One
  earlier run had a transient local connection-abort; the isolated failing
  logout test and the complete targeted selection both passed on rerun.
- Diagnostics and document-view Node tests: passed.
- JavaScript syntax checks for `app.js`, `citations.js` and `diagnostics.js`:
  passed.
- `sam validate --lint --template infra/cloudformation/phase-14-public-edge.yaml`:
  passed.
- `git diff --check`: passed.
- GitHub Actions run `36874101068` passed the final tested commit with 591
  Python tests (one Windows-specific skip), Node/document-view checks,
  JavaScript syntax checks and the offline browser gate. The original local
  shell did not have its Playwright module set, but the CI run used the pinned
  Playwright 1.62.1 environment and passed the approved synthetic journey.

## Publication procedure (completed)

After review, the Lambda zip was uploaded to the existing versioned artifact
bucket and a reviewed CloudFormation update change set was created using the
existing parameter values. `UsePreviousValue` was used for every unchanged
parameter and only the immutable application key/version changed. The change
set was executed after CI passed; its expected and observed changes were a
Lambda code update plus the dependent integration URI refresh, with no IAM
replacement, route replacement or data-plane resource change.

The five frontend files were extracted into a clean staging directory and
uploaded to the existing private, versioned frontend bucket. Invalidation of `/`,
`/index.html`, `/styles.css`, `/app.js`, `/citations.js` and `/diagnostics.js`
completed only after the reviewed update was executed.

Expected small costs are S3 requests/versioned storage and one CloudFront
invalidation. No new capacity, IAM permissions, inference or business-data
operation is part of this release.

## Deployment result

- Application/frontend artifact source commit: `3293580` (the final tested
  commit is `d52feb5`; its test-only changes do not alter either artifact).
- Lambda object: `phase-14/3293580/legaldesk-lambda.zip`, S3 object version
  `.9gggf7Tl1cXZ1TEAsm.5_zuard1tDuM`.
- Lambda `CodeSha256`: `JnhA8Lu3/fxkTI3EG8yty5PGiVlzjfighXGc3smtggA=`;
  deployed state `Active`, matching the candidate ZIP.
- CloudFormation `LegalDeskPhase14PublicEdge`: `UPDATE_COMPLETE`.
  Change set `logout-motion-3293580` executed successfully and was consumed
  by CloudFormation.
- Frontend object versions after upload:

| Key | Version |
|---|---|
| `index.html` | `r5SQAEXVtyn_87qjR0GEOS4I.jubHlIc` |
| `styles.css` | `a8gXlZX7F.yv.tKi_F8tyHm1Jq_nmOpF` |
| `app.js` | `BDWETZijNl2i8WTsoublOFyrM.RVRZ_4` |
| `citations.js` | `xgqLA5BOQatF22C4Of1bXVYN2m7fZOlM` |
| `diagnostics.js` | `uynQINZE3kGKtrkEJX.pUjMSoeagsa9U` |

- CloudFront invalidation `ID1FQ9ML4BUQE8WAW506DM8ETF` completed for `/`,
  `/index.html`, `/styles.css`, `/app.js`, `/citations.js` and
  `/diagnostics.js`.
- CDN verification returned `200` and exact manifest SHA-256 values for all
  five assets. `GET /logout` returned `302` with `Location: /`; unauthenticated
  `GET /api/me` returned `403`.

## Rollback

Keep the previous immutable Lambda artifact available for rollback:

- key: `phase-14/dfa85e43cc24bbe1e152ccc76423b1950ed1259a/legaldesk-lambda.zip`
- object version: `TSoCDD85EtPDArDp73KIC_wRkOXee.eX`

For the frontend, restore the pre-release object versions in the existing bucket:

| Key | Previous version |
|---|---|
| `index.html` | `CowPTAA4O4TrT1tbJrIZOOgPzdOXpvW0` |
| `styles.css` | `yzjTN4k2bB5shUqsBJnDBpF.I7ykKjye` |
| `app.js` | `5Unp82YGfVcWT89mQivqNAulR8l9By3m` |
| `citations.js` | `m9_Hw7ij4bLo7VK.ZnIlvE9VUTFzxVWd` |

The pre-release baseline has no `diagnostics.js` object. If the candidate adds
that object, restore that absence with the bucket's versioned delete-marker
procedure rather than deleting historical versions. Invalidate all six paths
above, restore the Lambda through a reviewed change set, and verify the public
root, logout landing and unauthenticated API response. Preserve the stack,
frontend bucket, logs and all existing data-plane resources; never delete user
documents or the stack as a rollback mechanism.

Execution and publication are complete. Rollback remains a reviewed operation:
restore the previous immutable Lambda/object versions above, restore the
pre-release frontend versions, invalidate all six paths, and verify the same
unauthenticated checks before resuming traffic.
