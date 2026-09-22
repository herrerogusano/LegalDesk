# Phase 13 — offline guided manual demo

This is a **test-only, local-only** walkthrough. It uses the concrete HTTP
application with provider doubles from `tests/phase13_browser_server.py`; it
does not call AWS, Bedrock, Cognito, S3, Gateway or any real model. The
fictional IdP redirect is intercepted only to complete the local PKCE fixture.
All other browser destinations must be loopback and are blocked otherwise.

## Start

Terminal 1:

```powershell
python tests/phase13_browser_server.py
```

Copy the `baseUrl` printed by the server, for example
`http://localhost:41231`.

Terminal 2:

```powershell
node tests/phase13_manual_browser.cjs http://localhost:41231
```

The helper opens a visible Chromium window and stays alive until the browser
is closed or Ctrl+C is pressed. It performs no business clicks. If Playwright
or the browser is not on the default Node path, use the same optional
variables as the automated acceptance runner:

```powershell
$env:PLAYWRIGHT_MODULE = "<existing Playwright module path>"
$env:BROWSER_EXECUTABLE = "<existing Chromium or Edge executable>"
node tests/phase13_manual_browser.cjs http://localhost:41231
```

The finite safety check is:

```powershell
node tests/phase13_manual_browser.cjs http://localhost:41231 --preflight
node tests/phase13_manual_browser_helpers.test.cjs
```

## Manual checklist

1. Click **Entrar**. The fictional local IdP redirect completes the test
   login; no username, password or real identity is used.
2. Select **Integration Matter** (`matter-integration`).
3. Choose `tests/fixtures/manual-demo-evidence.txt` and click **Autorizar
   subida**.
4. Wait until the document is shown as **INDEXED**.
5. Ask: `What is the inspection period?`
6. Expect an answer containing **four years** and status `answerable`.
7. Open the citation and verify the inspected passage is exactly:
   `The inspection period is four years.`
8. Click **Metadatos MCP** and inspect the authorized document metadata.
9. Click **Solicitar revisión** and confirm an open `reviewTaskId`.
10. Click **Ver auditoría** and confirm the session events include chat,
    metadata and review creation.
11. Confirm **Historial aceptado** contains the question and answer, while no
    storage URL is displayed.
12. Optionally ask `none: where is the missing emergency assembly point?` and
    expect `insufficient_evidence` with no citation.
13. Click **Cerrar sesión**, then close the browser and stop Terminal 1 with
    Ctrl+C.

The local fixture exposes one authorized matter. Cross-matter denial is
covered by the automated security tests and live smoke; this manual-only
helper deliberately adds no UI control or API shortcut for an unauthorized
matter.
