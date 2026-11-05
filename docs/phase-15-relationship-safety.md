# F-19 / F-35: exact source relationship rendering

Offline candidate, updated 2026-11-05. Owner chose exact source rendering on
2026-11-04 and answer-aware eligibility on 2026-11-05. **Both unchanged offline
gates now pass. Live behavior and semantic evasion resistance remain unverified.**
No AWS operation, model/Harness invocation, deployment or merge was performed.
The supervisor owns the failure register and vault decision log; neither was
edited by this worker.

## Why the prose replacement exists

D-024 arose from the role-reversal holdout: lexical overlap did not prove which
party owed which action. `actor/action/recipient`, supported in source order,
and backend rendering prevented reversed writer prose from reaching the user.
That protection remains: required projections never use writer prose, including
when the writer gives a correct triple but writes a reversed answer.

The old template could invent an obligation or a linking preposition. The new
renderer takes selected evidence explicitly, finds the ordered triple within
one source sentence and returns that exact whole sentence (outer whitespace
trimmed only). It retains modality, links, spelling, punctuation, line breaks,
prefix conditions and suffix exceptions. Multiple relationships selecting the
same source occurrence emit it once. Multiple matching occurrences, even
identical ones, are ambiguous and fail closed. Sentences from separate passages
cannot be combined to support a triple. Original supporting citations remain;
the complete output still crosses the production grounding adapter.

Projection is required only if **both** selected evidence and the writer's prose
trigger the same English/Spanish detector. An unrelated obligation elsewhere on
a page cannot force a non-obligation date/law answer to project. This eligibility
check runs after validating answer text and before deciding whether a malformed
relationship must fail closed or may be dropped. Detection vocabulary, sentence
rules and window logic are unchanged by the 2026-11-05 eligibility amendment.

When projection is optional, a valid relationship checks direction and leaves
prose unchanged. Optional malformed/unsupported relationships are dropped with
the existing accepted `write_answer` reason. Required missing, malformed,
reversed, unsupported, ambiguous or unbounded projections fail closed with
existing closed codes; ambiguity/bounds use `writer_relationship_unsupported`.
The #105/#107 shape handling remains inside the real Converse adapter path.

## Explicit sentence policy and bounds

- A run of `.`, `!` or `?`, followed by optional closing quotes/brackets and
  whitespace/end, terminates a sentence. Decimals and hierarchical numbers do
  not contain such a terminating interior dot.
- A closed abbreviation list protects common Spanish titles/references and
  English titles, including Sr./Sra., D./Dña., art./arts., núm., pág., apdo.,
  p. ej., S.A./S.L. and EE.UU. Title dots precede names; for other abbreviations,
  an uppercase continuation or end releases the final dot as a boundary. This
  deliberately avoids bridging `etc.` into the next capitalized sentence.
  Unknown abbreviations receive ordinary punctuation handling.
- Ordinary LF/CRLF line breaks are retained as OCR/line wraps. Blank lines and
  new numbered, lettered, Roman or bullet list items are hard boundaries. The
  marker is retained in a self-contained numbered/list source sentence.
- Commas, colons and semicolons remain in the source sentence to retain its
  conditions. A list dependent on an unfinished/colon header is unsafe to
  extract, including later sentences in its item. An unfinished fragment before
  a hard boundary also prevents the immediately following sentence from being
  published without its prefix. A modal-only Spanish list header still triggers
  projection, so this case fails closed rather than silently losing the gate.
- A source sentence must have a terminal boundary. Missing terminal punctuation,
  a triple crossing hard boundaries, split OCR words, ambiguous support or
  truncation fail closed; no OCR correction or hyphen repair is invented.
- Maximum whole source sentence: **2,000 characters**. Maximum combined output:
  **5,000 characters**, matching the existing Guardrails answer-content cap.
  Exceeding either bound fails closed; no clipping removes a condition.

This is deterministic segmentation, not a general linguistic parser. Unknown
abbreviations, broken OCR and dependent lists can cause safe extra abstentions.
It does not establish that a retrieved chunk contains every condition elsewhere
in the document. Source semantics outside the retrieved passage are unverified.

## Spanish detection and census

Evidence matching now tokenizes Unicode letters/digits and normalizes NFC.
Accents remain meaningful: `deberá` stays whole; `año` does not become `ano`.
Detection alone folds accents for the explicitly listed forms, accommodating
missing OCR accents without weakening source matching.

The bounded Spanish trigger covers deberá/deberán, debe/deben, habrá(n) de,
tendrá(n) que, tiene(n) que, se obliga(n) a and está/están obligado/a(s) a.
Its closed action families are pay/abonar, notify/avisar/comunicar, send/remitir,
deliver/proporcionar, transfer/ceder, return/restituir, indemnify/reimburse and
inform. The code explicitly enumerates infinitive/reflexive, third-person
present/future, subjunctive and gerund forms; it does not use unbounded stems.
A modal/action pair needs a following object token and at most eight intervening
tokens in one clause. Commas/semicolons/colons and selected adversative phrases
separate clauses for detection. Explicit third-person future directed actions
also trigger without a modal: legal payment/return commitments use those forms.
A descriptive future action may therefore conservatively trigger outside this
small corpus; no general semantic false-positive claim is made.

The established English detector still scans the whole passage without a token
window. It can pair unrelated clauses; this known broad trigger is unchanged.
Rendering now requires one bounded source sentence, so a cross-sentence triple
cannot be published. Spanish detection tests reject unrelated clauses and a
modal more than eight intervening tokens from the action.

Reproducible census: `python scripts/phase15_relationship_census.py`, using
`build_world`/the frozen corpus preparation source pages, not live extracted OCR.
Dataset SHA-256: `c88682195c3806db0210c5b3b47253f1e577d5d1425201aaa05bce42f80ada07`.

- 27 documents; 10 filenames contain `-es-`.
- 12 declare Spanish: those ten plus the negative-notice fixture and the empty
  failed-upload document. There are **11 nonempty Spanish texts**.
- The seven Spanish IDP fixtures contain one directed-obligation document,
  `doc-judgment-02-es-scanned`. Prepared extensions add the short Spanish notice
  and long Spanish lease: **three directed-obligation documents** in total.
- Detection: **0 → 3/3**, with **0/8 false positives** and **0/3 false negatives**
  against the independent document-level construction annotation. This is a
  fixture census, not measured general Spanish semantic accuracy.
- Controlled tests send all eleven texts through the real Converse resolver,
  writer, grounding adapter and chat, faking only Bedrock clients. The current
  answer-aware census makes **44 requests**: eleven texts times two prose cohorts
  (non-obligation date vs recognized Spanish obligation) times two relationship
  variants (omitted vs malformed).
- For the **non-obligation cohort**, omitted and malformed projections each cause
  **0/11 fail-closed answers**; all eleven malformed relationships are dropped
  with `writer_relationship_shape_invalid` on accepted `write_answer` events.
  The evidence-only 2026-11-04 candidate failed closed 3/11 in this cohort.
- For the **asserted-obligation cohort**, omitted and malformed projections each
  cause **3/11 fail-closed answers**, with the exact missing/shape stage reasons.
  The other eight evidence-negative cases stay optional; all eight malformed
  relationships retain their accepted drop reason. There are **zero unexpected
  errors** across all 44 requests. This is a control measurement, not proof that
  an arbitrary synthetic obligation is supported by the evidence.
- Actual live writer outputs and live answer-abstention rates were **not
  measured**. The corpus detection FP/FN counts above are unchanged and do not
  describe answer-level semantic detection accuracy.

## Grounding and untrusted content

The integration tests verify exact source text reaches `ApplyGuardrail` as the
answer, original evidence remains tagged `grounding_source`, citations remain
`citation-1`, and accepted numeric grounding/relevance scores reach the stage
event. The provider-shaped fake returns 0.92/0.80 for the accepted verbatim
sentence. Returning 0.2 grounding still rejects it; verbatim text never bypasses
the guardrail. **Whether real Bedrock scores a verbatim sentence that way is not
verified** and cannot be claimed from this offline test.

A separate injection sentence without the ordered triple is not selected as the
rendered answer. If an injection sentence contains the ordered triple, it can
be selected by this lexical rule, in full, including its malicious words. It
remains untrusted data in the unchanged writer prompt and Guardrails content;
it is never promoted into a system instruction or executed. The tests show
provider policy block hides it, while a provider allow can publish it. This
control does not semantically classify injection; live rejection is unverified.
No prompt wording/version/hash was changed and no new content logging is added.

## Answer-aware eligibility: evasion and residual risk

Bilingual real-adapter tests use the same page with an obligation and unrelated
date/law facts. Date/law answers without a relationship pass, while recognized
obligation answers without a relationship fail closed. Reversed relationships
still fail closed for recognized obligation prose; malformed/reversed optional
relationships are dropped with closed reasons. Required source extraction and
its boundary/bounds tests continue to use an explicit recognized obligation as
the test prose; the previous neutral helper default would no longer exercise
required rendering after the approved eligibility change.

The detector is lexical, not a semantic assertion classifier. These concrete
**reversed** obligations evade it and pass the projection control without a
relationship when the provider-shaped fake grounding response allows them:

- English evidence: supplier must send notice to buyer. Evasive prose:
  `The buyer has a duty to remit written notice to the supplier.` (`duty` is not
  a modal term), or `The buyer must dispatch written notice to the supplier.`
  (`dispatch` is not an action term).
- Spanish evidence: supplier must notify buyer. Evasive prose:
  `Incumbe al comprador notificar al proveedor.` (`incumbe` is not a modal
  construction), or `El comprador debe cursar un aviso al proveedor.` (`cursar`
  is not an action term).

These are deliberately red-team demonstrations, not examples of supported
answers. No detector vocabulary was expanded to hide this residual risk. In
this path, citation/authorization checks still constrain sources and the real
Guardrails adapter still evaluates the unchanged prose against selected source
content. Grounding is the remaining semantic support check; the lexical
projection control contributes no direction proof for these evasions. The fake
high-score response can accept all four. **Real Bedrock rejection has not been
verified and must not be assumed.** The unchanged English passage-wide trigger
also retains its previously documented broad-clause behavior; this update
reuses it identically on evidence and prose, rather than claiming a new English
linguistic classifier.

## Current verification (2026-11-05)

PowerShell: `$env:PYTHONPATH = 'tests;backend/src;agent/src'`.

`python -m unittest test_relationship_source_rendering test_relationship_census test_model_output_reasons test_chat`:
**Ran 83 tests, OK**. This includes the bilingual answer-aware tests and four
concrete evasion demonstrations through real adapters with Bedrock clients faked.

`python -m unittest test_relationship_source_rendering test_relationship_census test_evidence test_chat test_model_output_reasons test_grounding_scores test_phase13_grounding test_guardrails test_phase_11_observability test_phase14_holdout_runner test_phase13_holdout test_prompts test_retrieval test_phase13_journey_security test_phase15_search_documents test_phase15_agent_output test_phase15_routing_eval test_phase15_golden_eval`:
**Ran 384 tests, OK**. The old golden CLI/all-jobs/all-cases assertions now pass.

`python evals/phase15_routing_eval.py offline`: **30/30, PASS**.

`python evals/phase15_golden_eval.py offline`: **PASS**, no failed gates; 80 cases,
77 graded cases pass, three original expected known-limit failures unchanged,
async scenarios **29/29**. `lt-12`, `lt-20` and `am-05` all pass. For each case,
selected evidence triggers projection detection but the unchanged `GoldenWriter`
prose does not (zero recognized modal terms); its fact-only answer is preserved
and satisfies the frozen fact checks. In particular **lt-20 also passes**, rather
than being worked around or reclassified. Neither fake writer, frozen dataset,
expectations, gates nor thresholds changed.

`python scripts/phase15_relationship_census.py`: **44 controlled requests**, counts
above, same corpus SHA-256, zero unexpected errors and zero live model answers.

`git diff --check`: PASS. No linter/type-check configuration or infrastructure
change applies to these Python-only changes. Relevant authorization/retrieval
and cross-matter security suites are in the 384-test command above.

Historical verification of the evidence-only candidate (2026-11-04): 19 focused
tests passed; the broad run executed 379 tests with three golden assertions
failing because `lt-12`, `lt-20` and `am-05` failed at writer. Those results were
reported before the owner approved answer-aware eligibility. No old result
report was rewritten; the current runs are separate offline executions.

Changed old pins in this PR: `test_evidence` passes evidence to the renderer and
expects the exact sentence including articles; `test_chat` expects the same
whole source instead of the English template. `phase14_holdout_runner` passes
selected evidence to the new renderer argument, retaining reversal protection;
its tests pass. Frozen datasets, thresholds and gates, Public Edge template and
historical result reports remain unchanged.

No live AWS, model, Harness, IAM, Bedrock score, CloudWatch delivery, OCR/parser
or deployed-path verification was performed. No resources/costs were created.
After an approved release the search Lambda still needs a reviewed **Gateway
change set**; Public Edge code-only CD does not update it. No later phase began.
