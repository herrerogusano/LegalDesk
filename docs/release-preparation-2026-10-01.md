# Release preparation — 2026-10-01

Status: **prepared for review; not published**.

This record covers the approved workspace refinement release: review-motion and
workspace UI changes, expired-session logout recovery, diagnostics, catalog and
error metadata updates, plus the associated tests and offline CI. It does not
change RAG, prompts, retrieval, ingestion, Guardrails, model configuration,
IAM policy, data-plane resources or user data. No AWS write, artifact upload,
CloudFormation change-set execution, frontend upload or CloudFront invalidation
was performed in this preparation step.

## Inventory and deployment boundary

- Branch: `codex/workspace-refinement`; target branch: `developer`.
- AWS region: `eu-west-1`.
- Existing stack: `LegalDeskPhase14PublicEdge`, `UPDATE_COMPLETE`.
- Read-only inventory confirmed the existing API, six routes (including
  `GET /logout` and `POST /logout`), CloudFront distribution, Lambda, frontend
  bucket, logs and execution role. No new resource was created.
- The current public baseline returned `200` for the root/static legacy assets,
  `302` for `GET /logout`, and `403` for unauthenticated `/api/me`. The new
  `/diagnostics.js` path is not present in that baseline (`403`), so publication
  remains visibly pending review.

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
- The owner-provided offline browser acceptance remains the authoritative
  browser evidence for this workspace. It was not rerun during this release
  preparation because the prior PASS evidence already covers the approved UI
  changes; no new browser evidence is claimed here.

## Deferred change set and publication procedure

After review, upload the Lambda zip to the existing versioned artifact bucket,
record its immutable S3 object version, and create (but do not execute) a
reviewed CloudFormation update change set using the existing parameter values.
Use `UsePreviousValue` for every unchanged parameter and provide only the new
immutable application key/version. The expected application change is a
Lambda code update; no IAM replacement, route replacement or data-plane
resource change is expected. Review the change set before execution.

Separately extract the five frontend files into a clean staging directory and
upload them to the existing private, versioned frontend bucket. Invalidate `/`,
`/index.html`, `/styles.css`, `/app.js`, `/citations.js` and `/diagnostics.js`
only after the reviewed update is executed. These steps are intentionally
deferred until the supervisor sends the execution approval.

Expected small costs are S3 requests/versioned storage and one CloudFront
invalidation. No new capacity, IAM permissions, inference or business-data
operation is part of this release.

## Rollback

Keep the current immutable Lambda artifact available:

- key: `phase-14/dfa85e43cc24bbe1e152ccc76423b1950ed1259a/legaldesk-lambda.zip`
- object version: `TSoCDD85EtPDArDp73KIC_wRkOXee.eX`

For the frontend, restore the current object versions in the existing bucket:

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

Execution, publication and rollback remain gated on the supervisor's explicit
follow-up after reviewing this record and the PR.
