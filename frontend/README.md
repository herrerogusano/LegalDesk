# Frontend

The local loopback application serves `index.html` together with `styles.css`,
`citations.js`, `diagnostics.js`, and `app.js`. The small diagnostics module
normalizes allowlisted MCP/audit metadata before the application renders it. It uses the server-held session and CSRF token
returned by `/api/me`; it never stores a JWT in the browser. Matter selection,
upload authorization, document status, chat, citation inspection, bounded
history, MCP metadata, review requests, and audit display all use the real
HTTP API.

The constrained public beta is also available at
[`https://d3nxeyrpa3juwl.cloudfront.net`](https://d3nxeyrpa3juwl.cloudfront.net)
for pre-provisioned Cognito users and fictional/public documents only. It is
not an anonymous signup or a path for real legal/client data.

Document actions are deliberately split: **Actualizar estados** performs only a
fresh GET and reports the timestamp, changes, and current counts; **Preparar
para consulta (N)** is the explicit action that starts ingestion for at most 20
`UPLOADED` documents. Processing, incomplete, and failed records are never
silently retried. Motion uses shared CSS timing tokens plus the existing
details animation, and is disabled for users who prefer reduced motion.

Run the configured LegalDesk loopback entry point rather than opening this file
directly. The page remains dependency-free, responsive, keyboard navigable,
uses visible focus styles and live status announcements, and renders all
backend values as text. Citation inspection receives an expiring server handle
and does not expose source URIs or storage paths.

## Agentic chat (Phase 15)

When the backend has the agentic chat enabled, an open question returns `202` with a `jobId`.
`app.js` polls `GET /api/matters/{matterId}/chat-jobs/{jobId}` (shows "Pensando…" and, after about
45 s, "Está tardando más de lo habitual…"), and `citations.js` renders the `text`, `data` and
`inventory` blocks as text with their citation handles. Polling stops when the job ends, when the
matter changes, and on logout. Data lines link to the document's extracted-data panel (accepted
values) or to its pending IDP review (pending values); the agentic chat offers no review of its own
answers. With the feature off, the synchronous answer path is unchanged. Behavior test:
`tests/phase15_agent_chat_frontend.cjs`.
