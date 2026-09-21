# Phase 13 — application entry point

This document does not authorize AWS usage. The release remains
`NOT_READY_FOR_PROD`; local HTTP integration and UI acceptance use provider
doubles and do not establish live AWS or real-model semantic behavior.

## Local checks, no AWS

Install the two editable packages in the selected Python environment if needed:

```powershell
python -m pip install -e '.[aws]' -e agent
python -B -m unittest discover -s tests -q
python -m legaldesk --help
```

The tested environment uses Python 3.13.13, boto3/botocore 1.43.97 and PyJWT
2.14.0. SDK schema checks inspect local service models without invoking AWS.
Provider doubles live under `tests/`, never in the production composition.

For reproducible offline browser acceptance, start the explicitly test-only
server in one terminal:

```powershell
python tests/phase13_browser_server.py
```

It prints a loopback `baseUrl` with a random port. In another terminal, with
Playwright available and `BROWSER_EXECUTABLE` optionally pointing to Edge:

```powershell
node tests/phase13_browser_acceptance.cjs http://localhost:PORT_FROM_SERVER
```

`PLAYWRIGHT_MODULE` may point to the installed Playwright module if it is not
on Node's module path. The browser runner intercepts only the fictional IdP
redirect and the dedicated UI-error case; application/storage requests use
real local HTTP. It blocks non-loopback network destinations other than that
intercepted fictional IdP. Screenshots go to ignored `test-results/`.
Stop the fixture server with Ctrl+C afterward. This is a test harness, not a
production login substitute or a model-quality demo.

## Real application — only after separate AWS approval

The entry point is `python -m legaldesk` (also installed as `legaldesk`). It
refuses to construct AWS dependencies without the explicit `--allow-aws` flag.
Do not supply that flag during the current local-only phase.

The server binds loopback on port 8000. Open **http://localhost:8000** in the
browser, matching the existing Cognito callback `http://localhost:8000/callback`.
Do not start login through `127.0.0.1`: its host-scoped state cookie would not
accompany Cognito's redirect to `localhost`. This is a local demo, not public
HTTP hosting or a production TLS deployment.

Configure non-secret resource identifiers in the operator environment:

| Variable | Value to provide after inventory |
|---|---|
| `AWS_PROFILE`, `AWS_REGION` | Approved scoped operator profile; historical region `eu-west-1` |
| `LEGALDESK_METADATA_TABLE_NAME`, `LEGALDESK_SOURCE_BUCKET` | Existing/recreated metadata table and private source bucket |
| `LEGALDESK_KNOWLEDGE_BASE_ID`, `LEGALDESK_DATA_SOURCE_ID` | Dedicated bounded synthetic source and KB |
| `LEGALDESK_GUARDRAIL_ID`, `LEGALDESK_GUARDRAIL_VERSION` | Approved immutable Guardrail version |
| `LEGALDESK_RESOLVER_MODEL_ID`, `LEGALDESK_WRITER_MODEL_ID` | Approved Bedrock model/profile identifiers |
| `LEGALDESK_HARNESS_ARN`, `LEGALDESK_GATEWAY_URL` | Existing Harness and JWT Gateway MCP endpoint |
| `LEGALDESK_MEMORY_ID` | Existing short-term Memory, no long-term strategies |
| `LEGALDESK_JWKS_URL`, `LEGALDESK_OIDC_ISSUER` | Matching Cognito issuer/JWKS configuration |
| `LEGALDESK_OIDC_CLIENT_ID` | Public PKCE client, not the M2M service client |
| `LEGALDESK_OIDC_AUTHORIZATION_ENDPOINT`, `LEGALDESK_OIDC_TOKEN_ENDPOINT` | Configured Cognito HTTPS endpoints |
| `LEGALDESK_MATTER_CATALOG` | Comma-separated synthetic matter IDs; membership is still checked server-side |

Optional configuration: `LEGALDESK_OIDC_AUDIENCE`,
`LEGALDESK_REQUIRED_SCOPE` (retain `legaldesk/use`), and
`LEGALDESK_SYSTEM_PROMPT_PATH` (normally the versioned repository artifact).
Never put access JWTs, client secrets, AWS keys or legal documents in committed
configuration. Browser sessions keep the JWT server-side; the client receives
an opaque HttpOnly session cookie and a CSRF token.

Before enabling AWS, complete the [access preflight](phase-13-application-access.md)
and the [approved-smoke gate](phase-13-smoke-plan.md). All SDK clients are
configured for one total attempt. Stop the application after the bounded smoke;
no background polling, automatic replay or deployment is authorized here.

## Deliberate local limitations

Sessions, citation handles, the visible audit cache and the allowlist of
application-accepted Memory event IDs are bounded process-local state. Restarting
the application requires login and a new conversation; it does not grant access
to arbitrary existing provider events. Short-term Memory is used for accepted
conversation history, not as documentary evidence for subsequent questions.
Provider-written Harness/tool/rejected messages must not appear in that history.

No source deletion/abandoned-upload cleanup job, distributed session store,
long-term Memory, production hosting or release promotion is introduced.
