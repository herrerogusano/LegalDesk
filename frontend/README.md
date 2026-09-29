# Frontend

The loopback application serves `index.html` together with `styles.css`,
`citations.js`, and `app.js`. It uses the server-held session and CSRF token
returned by `/api/me`; it never stores a JWT in the browser. Matter selection,
upload authorization, document status, chat, citation inspection, bounded
history, MCP metadata, review requests, and audit display all use the real
HTTP API.

Run the configured LegalDesk loopback entry point rather than opening this file
directly. The page remains dependency-free, responsive, keyboard navigable,
uses visible focus styles and live status announcements, and renders all
backend values as text. Citation inspection receives an expiring server handle
and does not expose source URIs or storage paths.
