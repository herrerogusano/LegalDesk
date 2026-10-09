# IDP review integration contract

Status: implementation contract under approved ADR-020, **not deployed evidence**.
The authoritative requirements remain `PHASE_14_IDP_PLAN.md`.

## Machine creation boundary

Automatic creation must use the existing Gateway and Review target. It is an
additive IDP variant of `create_review_task`, not a fabricated accepted chat
answer or a direct Lambda call. Existing human calls keep their current contract.

1. The processing worker supplies only references to a durable document/run
   and field IDs; it cannot supply authoritative tenant, proposed values,
   evidence, actor, review status or an approval decision.
2. A short-lived invocation record binds the service client, immutable run,
   document/content identity, exact action and correlation. Keep machine
   invocation/grant namespaces distinct from human Gateway capabilities.
3. Gateway validates the Cognito token. The interceptor additionally requires
   the configured machine client, access-token use and exact IDP creation scope.
   A machine token must never fall through to human authorization.
4. The interceptor resolves the persisted run/document, checks scope and field
   references, and issues a short-lived purpose-bound grant. Caller selectors
   are compared, never promoted to authorization facts.
5. The Review target repeats grant expiry/purpose/client/run/document/content
   checks, loads the proposed fields/evidence server-side, and creates one
   idempotent task with explicit service-actor provenance.

Share task creation/storage primitives where appropriate, but never construct
a fake `VerifiedIdentity` or human `RequestContext` for the machine. The worker
cannot write human grants, approve/correct/reject fields, close tasks, or call
metadata/list/update tools with its creation scope. Do not grant runtime access
to `DescribeUserPoolClient`; bootstrap the one new secret out-of-band into the
exact SSM Standard SecureString and cache short-lived tokens without logging them.

## Human decisions and current values

Extend the existing authorized review workflow additively with individual field
decisions. Every decision requires a human JWT and current matter authorization;
machine capabilities are insufficient. Persist an immutable entry containing
reviewer, timestamp, action, original/proposed value, resulting value/presence,
document digest, source run/schema, evidence and a bounded reason. Validate source
anchors against the canonical version; do not turn arbitrary client citations
into document evidence.

Keep origin independent from acceptance. A quoted literal clause can require
review; a human-confirmed derived estimate retains its calculation provenance.
Rejected/unavailable fields are not usable as confirmed structured answers.

Resolve the latest applicable human-confirmed correction ahead of newer
unreviewed model output. Applicability includes the same document content and
compatible field/schema meaning; never carry a correction blindly onto changed
content. Preserve both histories and surface conflicts. Order model runs by a
durable run/generation timestamp rather than which worker finishes last.

## Additive application contracts (local integration under review)

Explicit document metadata continues to use backend → Gateway → MCP, with
optional `historyLimit` (1–20) and an opaque `historyCursor`. The current
document/run projection is independent of the history page. History is a
bounded authorized `Query`, not a table scan or a replacement for the current
generation pointer.

The existing `/api/chat` request accepts the optional pair
`selectedDocumentId` and `selectedFieldName`; neither may be supplied alone.
The field is registry-allowlisted and the selected document is reauthorized.
Usable IDP fields retain the readable `answer`, existing evidence status and
citations, with additive structured IDP metadata. Missing, skipped, failed or
unusable IDP must fall back to RAG filtered to that exact authorized document;
the fallback is never written back as an accepted IDP field.

Human field decisions extend the existing `update_review_task` action with
`idpDecision` containing field, approve/correct/reject action, required reason,
evidence and, for corrections only, the raw proposed value. Actor, content
identity, origin, acceptance and resulting audit entry remain server-owned.
Persist normalized page text as an immutable run-scoped artifact so a new
human evidence anchor can be checked without repeating OCR. Validate the
artifact scope/digest and the current canonical source bytes before exposing
or applying results; historical evidence does not establish current validity.

This section describes the integration being validated, not deployed proof.

## Required evidence before enabling

- Machine creation traverses Gateway; human chat-review regression still passes.
- Wrong client/scope/tool, arbitrary fields/values, expired/replayed/forged grants
  and cross-matter references fail closed before target access or mutation.
- Actor and correlation survive both interceptor and target.
- Human field approve/correct/reject are authorized and immutable; reruns do not
  overwrite applicable confirmations, and machine tokens cannot perform them.
- Real deployed Gateway schema/authorizer and IAM changes are reviewed separately
  from code-only CD, with bounded smoke and rollback evidence.
