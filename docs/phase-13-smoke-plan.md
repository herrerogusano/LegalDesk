# Phase 13 — real E2E smoke design (one authorized attempt consumed)

Subsequent authorization: the user approved one bounded smoke on 2026-09-21.
See `phase-13-live-smoke.md` for the live gate, inventory and actual execution
record. The original design/caps below remain the scope, not proof of execution.
That attempt stopped before login and its temporary resources were removed.
Do not execute again from this document; a fresh attempt requires authorization.

This is a preparation document, not an executable approval. Reconcile the
request limits below with the completed local application before execution.
Local integration, security tests and complete regression suite must pass.
Neither a live inventory nor a deployment was performed while preparing this.

The application enforces per-invocation token/iteration limits, bounded sync
polling and zero SDK retries. The aggregate run ceilings below are operator
stop limits, **not** an implemented account-wide billing limiter. Before a live
run, record counters around the approved SDK requests and inspect managed
Harness usage; if an aggregate cannot be observed/bounded, do not execute it.
The browser does not automatically retry failed uploads, chat or tools.

## Scope and entry gates

- Account/region must be explicitly confirmed before any deployment; historical
  region is eu-west-1. No new region, model, paid feature or architecture.
- Two fictional users and two isolated matters. One newly generated fictional
  PDF in Matter A (at most two pages, 100 KiB, 2,000 extracted characters).
  No confidential/legal client data, OCR service or custom parsing service.
- Document a source-backed factual question and an unrelated absent-fact
  question before running. Do not alter expected answers after observing output.
- Record exact Git commit, configuration, model/profile, prompt hashes and
  resource IDs. Ensure no payload logging and no SDK/HTTP debug logging.
- Record current resource inventory/change sets separately. Existing stacks
  must not be blindly recreated or deleted. Verify public Cognito callback,
  memberships, JWT Gateway client/scope and temporary invocation bindings.
- Disable automatic application/SDK retries (total_max_attempts=1). Harness
  internal behavior is provider-managed: do not assert control of undocumented
  retry behavior; stop if observed counts exceed the approved budget.
- The versioned Review, interceptor and MCP Lambda configurations set
  `AWS_MAX_ATTEMPTS=1` / `AWS_RETRY_MODE=standard`. These are pending template
  changes, not changes to deployed functions; verify their effective values
  after the separately approved deployment.

## Single-run sequence

### Fixed fictional inputs (do not tune after observing output)

Generate one text-only, one-page PDF named `fictional-storage-note.pdf` with:

> Synthetic portfolio exercise. Northbridge Archive Ltd provides fictional
> storage services to Acme Orchard Ltd. Acme Orchard Ltd must pay Northbridge
> Archive Ltd within 23 calendar days after receipt of an invoice.

Factual question: `How long does Acme Orchard Ltd have to pay after receipt of an invoice?`
Expected: 23 calendar days, `answerable`, valid citation to this uploaded
document, inspection displaying the actual supporting payment sentence.

Absent-fact question: `Where is the emergency assembly point?`
Expected: `insufficient_evidence`, no invented location, no citations when
none of the retrieved passages materially supports the answer. A provider or
grounding error does not count as successful documentary absence.

Metadata action: `list_matter_documents` for Matter A. Review action:
`create_review_task` with `reasonCode=user_requested_review`. Expect exactly
one persisted OPEN task attributed to User A. Matter B is a distinct synthetic
matter inaccessible to User A, not a guessed production identifier.
These inputs are separate from the frozen semantic holdout; this small smoke
does not execute or establish the holdout's fourteen semantic expectations.

### Execution

1. User A logs in through configured Cognito Authorization Code + PKCE.
2. Select Matter A; prove the backend-derived user/matter scope.
3. Initiate one upload, PUT the synthetic PDF, confirm it through S3 HEAD.
4. Start one ingestion job; bounded polling until INDEXED. Stop on partial,
   unknown or failed status, or timeout. Never manually edit document status.
5. Ask the fixed factual question through the application; inspect response
   status, cited document/page/section and authorized supporting evidence.
6. Ask the fixed absent-fact question; expect insufficient_evidence without
   fabricated facts/citations. A technical failure is not a successful refusal.
7. Through the user-scoped Harness→Gateway path, list Matter A metadata.
8. Through the same path create one review with an explicit user request;
   independently verify the persisted creator/matter without reading bodies.
9. Attempt Matter B as User A. Must deny before business retrieval/mutation.
10. Inspect the correlation records and effective prompt identities for the
    factual operation and linked tool operations. Do not infer tool execution
    from model prose. Record pass/fail and closed diagnostics only.
11. Logout, stop the local application and apply the separately approved cleanup.

## Proposed execution ceilings

These are ceilings, not targets. A failure stops the run; no automatic rerun.
Final implementation must expose counters/preflight checks before approval.

| Operation | Maximum |
|---|---:|
| Login / code exchange | 2 attempts total, no refresh loop |
| Newly uploaded PDF | 1 |
| Data-source ingestion job | 1 |
| Ingestion status checks | 20, 15 seconds apart, at most 5 minutes |
| Application chat questions | 2 |
| Bedrock Retrieve | 2 |
| Direct Converse resolver/writer attempts | 4 total, at most 2 per question |
| Harness invocations | 2, each maxIterations=3, timeoutSeconds=120 |
| Model attempts including Harness iterations | 10 total |
| Model output | 512 tokens/attempt; no extended thinking or caching |
| Budgeted model input, including history/tool overhead | 160,000 tokens total |
| ApplyGuardrail | 8 requests, at most 20 text units each per enabled filter |
| Gateway lifecycle/discovery/business requests | 40 total; 2 intended business operations |
| Review creations | 1 successful task |
| Memory writes | 20 short-term events; 20 read requests |
| DynamoDB application operations | 400, bounded small items including repeated server-side authorization |
| S3 object operations | 30, including cleanup; no full bucket scans |
| Vector entries | 20 max in dedicated synthetic source/index |
| New log ingestion | 1 MiB of metadata only |
| New artifacts/data retained | 24 hours maximum before approved cleanup |

Bounded polling is explicit status observation, not retrying a failed operation.
Control-plane deployment/inventory/teardown calls must be enumerated separately
after reviewing the actual retained-resource inventory. Do not claim these
data-plane caps bound unknown pre-existing account usage or provider internals.

## Preliminary incremental cost estimate (USD, before tax)

Public prices checked 2026-09-21; recheck regional terms before execution.
For Sonnet 4.6 geo cross-region Standard, the published input/output prices
are $3.30/$16.50 per million tokens. The budgeted 160,000 input and 5,120 output
tokens imply about **$0.613** of inference. This is an estimate, not a billing
cap: token metering must include managed history and tool schemas.

Using published AgentCore rates, a deliberately conservative compute allowance
of 1 vCPU-hour plus 4 GB-hours is about **$0.1273**. Forty Gateway calls are
about **$0.0002** and 20 short-term Memory events about **$0.005**. Reserve
**$0.10** for Guardrail policies on the bounded text. The sum of these modeled
items is under **$0.85**.

Reserve another **$1.15** for small-volume embeddings/vector storage, S3,
DynamoDB, Lambda, Cognito, artifacts, logs and metrics over 24 hours. This is
an allowance, not a separately verified price quote for every service.
Preliminary expected incremental envelope: **under $2**; proposed conservative
approval ceiling: **$5**, conditional on the final inventory and request caps.
No actual spend has been queried. Existing retained-resource charges, tax,
currency conversion, longer retention and unrelated account activity are excluded.
If the inventory/configuration cannot support these assumptions, revise the
estimate and seek authorization before running; do not claim a guaranteed max.

Pricing sources:
- [Anthropic published Bedrock list prices, Sonnet 4.6](https://www-cdn.anthropic.com/files/4zrzovbb/website/3684c2faafb97418665782cea0001f439f74b1d2.pdf)
- [AWS AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [AWS Bedrock/Guardrails pricing](https://aws.amazon.com/bedrock/pricing/)

## Resources and teardown

Use the candidate inventory in PLAN_13; its retained/torn-down descriptions
are historical, not live status. Prefer existing Cognito, Gateway, Lambda,
Memory, table and Harness resources after verifying compatibility. Recreate
KB/vector/source/Guardrail resources only if inventory proves they are absent.
No custom Runtime, queue, dashboard or additional identity service is selected.

Do not blindly redeploy the original Phase 02 template: it creates both a bucket
and a table, whereas later phases may retain a separately recreated shared
metadata table. The approved change set must reuse that verified table and
restore only the missing source dependencies, without creating a competing
authorization store or changing tool/table wiring implicitly.

Application invocation bindings reuse the metadata table and expire for
authorization after five minutes. `expiresAt` validation is not deletion:
the current table does not configure DynamoDB TTL. Remove the recorded synthetic
bindings/grants during approved teardown; automatic abandoned-upload or grant
cleanup is not implemented in this phase.

Before execution, attach exact stack/resource IDs and decide keep/delete for
each resource. Cleanup: stop application/inferences; remove authorized smoke
review/membership/conversation/invocation records and short-term events as
appropriate; remove only the recorded synthetic object versions/sidecars;
remove vectors via approved sync or dedicated test-index teardown; remove newly
created smoke stacks in dependency order. Preserve retained shared resources
unless their deletion was expressly approved. Inventory managed secrets,
artifact versions, log groups and metric filters separately; verify teardown
completion instead of assuming a stack deletion cleaned everything.

## Authorization gate

STOP. Present final local results, resource/change list, enforced request caps,
reconciled cost estimate and exact cleanup targets for explicit authorization.
Do not invoke AWS or promote developer to prod on the basis of this document.
