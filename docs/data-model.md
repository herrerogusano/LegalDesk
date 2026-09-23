# Data model and ownership

## Entities

| Entity | Authoritative owner/scope | Important relationships |
|---|---|---|
| `User` | Verified identity subject | May belong to one or more tenants and matters |
| `Matter` | `tenantId` | Explicitly lists authorized users |
| `Document` | `tenantId` + `matterId` | S3 key repeats both scopes as defense in depth |
| `Conversation` | `userId` + `matterId` | A session cannot be reused across matters |
| `ReviewTask` | `tenantId` + `matterId` | Creator must be authorized for the matter |

`ReviewTask` stores the closed reason, lifecycle timestamps, due date, optional
bounded notes, and a validated accepted-answer snapshot: question, answer,
evidence status, prompt hashes when available, and exact citations with
document/page/section/passage. It never stores an S3 key, source URI, or full
document. `list_review_tasks` uses a metadata-only Query projection; the UI
fetches the snapshot with `get_review_task` when a row is opened. `closed` is
terminal for now and retention/archival after closure remains a production
policy gap.

IDs are opaque, stable strings. IDs reveal no entitlement: possession of an ID
never grants access. Stored ownership/membership is the source of truth.

## Request scope derivation

1. The identity layer verifies token signature, issuer, audience, and expiry.
2. The backend maps the verified `sub` to its stored `User`.
3. A browser may request a `matterId`, but it is only an untrusted selector.
4. The backend loads the `Matter`, verifies bilateral membership and active
   status, then derives `tenantId` from that stored record.
5. The resulting immutable `RequestContext` is passed to retrieval, tools,
   memory, and audit code.

The browser does not supply an effective `userId` or `tenantId`. Retrieval and
tools must reject raw browser scope and accept only the server-built context.

## Planned storage keys

- S3 original: `tenants/{tenantId}/matters/{matterId}/documents/{documentId}/original.txt`
  or `original.pdf`, chosen from the validated media type. Bedrock metadata is
  stored beside it as `{source-key}.metadata.json` and never embedded as text.
- Public upload quarantine: `quarantine/tenants/{tenantId}/matters/{matterId}/documents/{documentId}/original.txt`
  or `.pdf`. `Document.s3Key` remains the canonical source key and
  `quarantineS3Key` is the server-owned upload key. The Bedrock data source
  includes only `tenants/`, never `quarantine/`.
- Metadata partition: `TENANT#{tenantId}#MATTER#{matterId}`
- Authorization User record: `pk=AUTH#USER#{verifiedSubject}`, `sk=PROFILE`
- Authorization Matter record: `pk=AUTH#MATTER#{matterId}`, `sk=PROFILE`
- Review task record: `pk=TENANT#{tenantId}#MATTER#{matterId}`,
  `sk=REVIEW#{reviewTaskId}`
- Gateway authorization grant: `pk=GATEWAY#GRANT#{grantId}`, `sk=PROFILE`;
  stores only verified subject, requested matter, correlation ID, target tool,
  and a five-minute `expiresAt` epoch checked by the consumer. Grants are
  replayable during that TTL; review idempotency limits duplicate writes. It
  contains no document body or client-provided scope. Automatic deletion of
  expired grant records is a deferred operational cleanup gap.
- Conversation scope is derived server-side from the authorized user/matter and
  opaque conversation/session selectors. The AgentCore values are deterministic
  opaque IDs, not the raw `{userId}:{matterId}:{sessionId}` string.
- Conversation binding records reuse the existing metadata table with
  `pk=CONVERSATION#{tenantId}#{matterId}#{userId}#{conversationId}` and
  `sk=SESSION#{sessionSelector}`. Creation is conditional and reads require an
  exact user/tenant/matter/conversation/session match; no new table is needed.
- Memory actor/session namespaces include the authorized user and matter only
  through the server-side derivation; raw tenant, user, matter, or browser
  selectors are never sent as AgentCore IDs.

## Public document safety gate

`Document` stores `quarantineS3Key`, `malwareScanStatus`, `malwareScanETag`, and
`malwareScanVersionId`. New uploads start as `PENDING`; only a corroborated
`NO_THREATS_FOUND` result can create the Bedrock metadata sidecar and promote
`PENDING_UPLOAD` to `UPLOADED`. Ingestion rejects every document whose scan is
not clean, regardless of its lifecycle status. Threat, unsupported, access
denied, and failed results delete the original/sidecar and persist `FAILED`.
Missing, malformed, cross-scope, stale-object, or uncorroborated events fail
closed without promotion. TTL/retention and production quarantine policy are
operational concerns; they are not authorization checks.
