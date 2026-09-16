# Frontend

Phase 04 adds a no-dependency citation panel at `index.html`. Its sample answer
and document names are explicitly fictional. The panel can also render a
backend-shaped response through `window.LegalDeskCitationPanel.renderChatResponse`
and uses DOM text nodes so answer and citation text is not interpreted as HTML.
It is a local presentation example; it is not connected to an API, login,
upload, or persistent conversation store.

Open `index.html` directly or serve this directory locally with
`python -m http.server 8000`. The layout is responsive, keyboard navigable,
uses visible focus styles and live answer announcements, and loads no external
fonts, scripts, or assets. It presents the question, evidence status, response,
legal disclaimer, and document/page/section citation metadata.

Values submitted by a browser remain untrusted until the backend authorizes
them. The page's fabricated demonstration response is not a legal source.
