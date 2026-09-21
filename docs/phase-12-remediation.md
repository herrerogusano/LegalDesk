# Phase 12 remediation design

## Evidence pipeline

Phase 12 now distinguishes three boundaries:

1. Authorization builds the server-owned request context and runs before
   retrieval. A denied context cannot reach Knowledge Base retrieval, the
   Evidence Resolver, or the Answer Writer.
2. The Evidence Resolver returns only the strict object
   `coverage=complete|partial|none`, `conflict=boolean`, and
   `supportingCitationIds`. IDs must be retrieval-issued IDs from the current
   authorized matter. The backend derives `evidenceStatus`: conflict always
   means `ambiguous`; otherwise complete means `answerable`; partial/none means
   `insufficient_evidence`.
3. The Answer Writer receives the validated resolution and writes answer text
   only. It cannot set status or citation IDs. For `none`, the writer is not
   called and the canonical insufficient-evidence response is returned.

The old combined `answer/citationIds/evidenceStatus` generator remains only as
a compatibility adapter for Phase 05 tests and providers during migration. New
integrations must pass `evidence_resolver`, `answer_writer`, and the
post-writer `grounding_validator` together. A `none` resolution skips the
writer; every answer-producing resolution must pass grounding validation.
The writer request explicitly says to return answer text only: status and
allowed citation IDs are fixed by the resolver/backend contract.

Retrieved passages are untrusted as instructions: document text cannot change
roles, reveal prompts, or authorize tools. They are nevertheless the
authoritative documentary evidence for claims when the backend has retrieved
them under the verified matter scope. “Untrusted” therefore describes control
flow, not a reason to discard a directly supported fact.

## Converse schema enforcement

The local resolver exposes the Bedrock Converse `outputConfig.textFormat` JSON
Schema payload shape for the resolver object. Server-side validation remains
mandatory because provider schema enforcement does not authorize citation IDs
or prove semantic entailment. Native Anthropic Bedrock citations are not
enabled: the structured-output contract uses LegalDesk's own
`supportingCitationIds` and these features are not combined in this design.

The checked-in code does not call Bedrock while validating this design. The
provider shape is based on the local botocore SDK and the reviewed AWS
documentation; model/region availability and a live response remain a future,
separately approved smoke.

## Synthetic debug and real-run proposal

`EVAL_DEBUG_SYNTHETIC=true` enables only the fixture-only local report in
`evals/synthetic_debug.py`. Because all inputs are synthetic allowlisted
fixtures, the report includes each synthetic question/passages, raw and
normalized resolver output, raw writer output, final answer, citation IDs,
grounding score, prompt metadata, model label, and validation/error codes. It
stores no chain-of-thought, secrets, or tokens, writes to a new path, and makes
zero AWS/model calls. Real Harness smoke refuses to run while this flag is enabled. It accepts
no output path outside the repository's `evals/` fixture area. Historical
reports are never modified.

`evals/phase12_remediation_runner.py` preflights nine samples:
three factual, three partial, and three injection cases. Separation requires a
resolver call plus a writer call per sample, so the maximum is 9 resolver + 9
writer = 18 model invocations, one attempt each and zero retries. The original
nine-inference proposal is incompatible with two model stages unless the writer
is removed or fused. Real execution requires both `--execute` and
`--preflight`, fixes SDK retries to one total attempt, and refuses to overwrite
historical or prior remediation evidence.

That 18-call maximum assumes the post-writer grounding result is supplied by a
deterministic synthetic evaluation oracle and therefore makes zero provider
calls. A managed Guardrails contextual-grounding check or a model-based
grounder would add another independently billable stage and requires a revised
budget and separate approval. Prompt `1.3.0` (SHA-256
`de28c6e7d7b3a9284cfac505e4f4d099e8854da9ce8ecebb7911c0adefe8af56`)
was executed once with the separated pipeline. The bounded run made 9 resolver
and 5 writer calls (`14` total), with zero retries, and accepted `3/9`. All
three factual cases passed; the partial and injection groups remain incomplete.
No rerun is permitted without separate approval.

## Harness evidence limits

`agent/src/legaldesk_agent/trace_evidence.py` normalizes only allowlisted
metadata already supplied by local fakes or application interceptors. A
cross-matter acceptance requires explicit `DENY`, `CROSS_MATTER`, and
`targetInvoked=false`. Text keywords and `InvokeResult.text` are always
inconclusive. This collector does not claim to expose the provider-owned
AgentCore/CloudWatch trace; validating that trace requires a future authorized
inspection with tracing and retention controls, neither of which is enabled by
this remediation.
