# Data model and ownership

## Entities

| Entity | Authoritative owner/scope | Important relationships |
|---|---|---|
| `User` | Verified identity subject | May belong to one or more tenants and matters |
| `Matter` | `tenantId` | Explicitly lists authorized users |
| `Document` | `tenantId` + `matterId` | S3 key repeats both scopes as defense in depth |
| `Conversation` | `userId` + `matterId` | A session cannot be reused across matters |
| `ReviewTask` | `tenantId` + `matterId` | Creator must be authorized for the matter |

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
- Metadata partition: `TENANT#{tenantId}#MATTER#{matterId}`
- Authorization User record: `pk=AUTH#USER#{verifiedSubject}`, `sk=PROFILE`
- Authorization Matter record: `pk=AUTH#MATTER#{matterId}`, `sk=PROFILE`
- Review task record: `pk=TENANT#{tenantId}#MATTER#{matterId}`,
  `sk=REVIEW#{reviewTaskId}`
- Gateway authorization grant: `pk=GATEWAY#GRANT#{grantId}`, `sk=PROFILE`;
  stores only verified subject, requested matter, correlation ID, target tool,
  and a five-minute `expiresAt` epoch checked by the consumer. It contains no
  document body or client-provided scope. Automatic deletion of expired grant
  records is a deferred operational cleanup gap.
- Conversation scope is derived server-side from the authorized user/matter and
  opaque conversation/session selectors. The AgentCore values are deterministic
  opaque IDs, not the raw `{userId}:{matterId}:{sessionId}` string.
- Memory actor/session namespaces include the authorized user and matter only
  through the server-side derivation; raw tenant, user, matter, or browser
  selectors are never sent as AgentCore IDs.
