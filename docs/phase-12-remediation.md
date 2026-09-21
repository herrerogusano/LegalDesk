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

The general product prompt `1.3.0` is retained in preflight as repository
provenance, but it is not sent to either separated model stage. The actual
resolver and writer versions and hashes are independently pinned and preflight
fails if either changes. The exact nine fixture IDs and ordering are pinned as
well; category counts alone cannot substitute another sample set.

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

## Resolver/writer isolation after the 3/9 run

The `3/9` result identified a concrete prompt-boundary defect: the resolver
was schema-constrained but still received the broad product conversation
prompt. That prompt contains answer-writing, legal-caution, refusal, and legacy
JSON rules, so the model could satisfy the resolver schema while applying the
wrong semantics—especially by discarding an entire passage that contained an
embedded instruction.

The local solution gives each model stage a dedicated, versioned contract:

- Evidence Resolver `1.0.0`, SHA-256
  `af14a60ec2c15e23b0cb1bf36f8374d768e72cad40b90ad2260a988ea9166984`;
- Answer Writer `1.0.0`, SHA-256
  `5a03f9b51fa18a3956c3050f3829de73f4b9b8548c1a1aaa868b0ad154adac66`.

The resolver contract says that questions and passages cannot override system
instructions, while passages remain authoritative factual evidence. It
explicitly requires ignoring an embedded directive without discarding relevant
facts from the same passage. Both stages receive their data as JSON rather than
breakable text delimiters. The writer contract cannot classify evidence or
choose citations and has status-specific rules for complete, partial, and
conflicting evidence.

The fixture-owned grounding oracle no longer compares an answer with one
canonical sentence. It validates typed claims and absence constraints against
the actual cited passage: required dates, amounts, and quantities must occur in
both evidence and answer; unsupported typed or lexical claims, missing
uncertainty, invalid citations, spec/evidence drift, and echoed injection
directives declared by the fixture (including stopword-shortened echoes) fail
closed. This does not claim synonym-level detection of arbitrary injection
paraphrases. The server-side grounding contract also requires the
validator to account for every supporting citation before those citations can
reach the final response. The writer-shaped fixture output passes the same
strict answer-only validator as the Converse adapter. This bounded oracle may
conservatively reject valid phrasing and is not a claim of general production
semantic grounding; the production output Guardrail remains an independent
boundary.

Runner `5.1.0` was executed once with dedicated stage prompts `1.0.0`. Its
immutable metadata-only report is
`evals/results/phase12-remediation-resolver-v2-report.json`: `5/9` accepted, 9
resolver plus 6 writer calls, and zero retries. The result isolated two
resolver errors between `partial` and `none`, one partial writer grounding
failure, and one injection fact downgraded from `complete` to `partial`.

The next remediation versions the resolver and writer as `1.1.0`. Its rules
are generic rather than fixture-specific: a related passage that establishes a
subject or relationship but omits the requested attribute is `partial`; only
materially unrelated evidence is `none`; and instruction-like text adjacent to
a supported fact does not reduce coverage. The writer is limited to the
supported relationship plus the explicitly missing detail. The oracle also
returns closed metadata-only failure codes while its production-facing
validator shape remains unchanged. Runner `6.0.0` pins these hashes and writes
only to a new path. The local report
`evals/results/phase12-solution-v2-synthetic-report.json` passes `9/9` with zero
AWS calls. Runner `6.0.0` was then executed once: its immutable report
`evals/results/phase12-remediation-resolver-v3-report.json` records `6/9`, 18
calls, and zero retries. Resolver classification passed all nine cases. The
three remaining failures are exclusively writer/oracle outcomes, identified
without raw text as `UNSUPPORTED_LEXICAL_CLAIM`, `UNCERTAINTY_MISSING`, and
`REQUIRED_VALUE_MISSING`.

Writer `1.2.0` now explicitly preserves a requested value together with its
unit, denomination, or full date and uses a constrained structure for partial
evidence. The oracle accepts bounded omission paraphrases and number/unit word
order while continuing to reject invented typed and lexical claims. The local
report `evals/results/phase12-solution-v3-synthetic-report.json` passes `9/9`.
The writer-only follow-up pins the resolver-v3 report by SHA-256, makes no
resolver calls, caps execution at 9 writer calls with zero retries, and writes
only metadata to a new immutable path. It was executed once and produced
`evals/results/phase12-remediation-writer-v1-report.json`: `8/9`, 9 calls, zero
retries. Only partial-03 failed, with `UNSUPPORTED_LEXICAL_CLAIM`.

The next correction changes no model prompt. The fixture oracle canonicalizes
only explicit grammatical variants of neutral evidence-relation tokens. It
does not stem arbitrary words, weaken typed-value checks, or permit invented
names/entities. The targeted follow-up pins the immutable `8/9` report by a
canonical JSON hash and permits one call for exactly partial-03, with zero
retries and metadata-only output. It has not been executed.

The repository exposes the separated path through `answer_question` only when
resolver, writer, and grounding validator are supplied together; partial wiring
is rejected. There is no deployed backend composition root in this repository,
so the bounded runner validates these boundaries but is not evidence of a
deployed product request end to end. The legacy combined generator remains a
documented migration adapter, not the target integration.

## Harness evidence limits

`agent/src/legaldesk_agent/trace_evidence.py` normalizes only allowlisted
metadata already supplied by local fakes or application interceptors. A
cross-matter acceptance requires explicit `DENY`, `CROSS_MATTER`, and
`targetInvoked=false`. Text keywords and `InvokeResult.text` are always
inconclusive. This collector does not claim to expose the provider-owned
AgentCore/CloudWatch trace; validating that trace requires a future authorized
inspection with tracing and retention controls, neither of which is enabled by
this remediation.
