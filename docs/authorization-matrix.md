# LegalDesk authorization matrix

Authorization is evaluated from a verified identity and server-side records.
`tenantId`, `userId`, and effective `matterId` are never accepted as browser
authority. Every target denies before its repository/provider call when the
subject is unknown, membership is absent, or a selector is cross-matter.
The only public context guard is `require_authorized_context`; it accepts only
the exact seal produced by `build_request_context`. Direct construction and
factory calls without the private token remain untrusted.

| Surface | Identity source | Server binding | Allowed scope |
|---|---|---|---|
| Upload/list/retrieval/citations | Cognito/OIDC or Gateway verified edge | User + tenant + matter | authorized matter only |
| Review | Gateway CUSTOM_JWT + short-lived grant | grant tool/TTL + membership reload | exact requested matter |
| MCP | Gateway grant | grant tool/subject/matter/TTL + membership reload | metadata tools only |
| Memory/chat | verified identity | conversation/session binding | exact user/tenant/matter/session |
| Direct Harness | IAM service identity | typed server-derived Memory scope | no browser actor ID |

Guardrails and retrieval filtering are defense in depth; neither replaces this
authorization decision. Phase 11 owns the operational audit view.
