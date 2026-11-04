# F-19 / F-35: exact source relationship rendering

Offline candidate, 2026-11-04. Owner chose option A (exact source rendering).
**Not accepted for release: the unchanged golden gate has three new failures.**
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
  writer, grounding adapter and chat, faking only Bedrock clients. With omitted
  relationships, **3/11 now fail closed**; with malformed relationships, the
  same **3/11 fail closed**, while eight optional cases retain prose/drop reasons.
  Before detection these three would follow the optional path. Actual live
  writer outputs and live answer-abstention rates were **not measured**.

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

## Verification and acceptance blocker

PowerShell: `$env:PYTHONPATH = 'tests;backend/src;agent/src'`.

`python -m unittest test_relationship_source_rendering`:
**Ran 18 tests, OK** (including 64 modal/action combinations, eight conjugated
forms, six standalone futures, real-adapter required/optional behavior,
abbreviations, linewraps, lists, bounds, reversal, grounding and injection).

Final focused check after adding stage-reason assertions to the census:
`python -m unittest test_relationship_source_rendering test_relationship_census`:
**Ran 19 tests, OK**; zero unexpected errors in the controlled census.

`python -m unittest test_relationship_source_rendering test_relationship_census test_evidence test_chat test_model_output_reasons test_grounding_scores test_phase13_grounding test_guardrails test_phase_11_observability test_phase14_holdout_runner test_phase13_holdout test_prompts test_retrieval test_phase13_journey_security test_phase15_search_documents test_phase15_agent_output test_phase15_routing_eval test_phase15_golden_eval`:
**Ran 379 tests; 376 passed, three failed**. The three failures are the golden
CLI-exit-zero, all-jobs-succeed and all-cases-pass assertions; the unchanged
simulated GoldenWriter emits no required relationships for the newly detected
Spanish evidence. These assertions remain unchanged and red.

`python evals/phase15_routing_eval.py offline`: **30/30, PASS**.

`python evals/phase15_golden_eval.py offline`: **FAIL** at frozen cases `lt-12`,
`lt-20`, `am-05` (`writer`); 80 cases, 77 graded, 74 graded cases pass, three
pre-existing expected known-limit failures remain, async scenarios **29/29**.
The new failures are not reclassified as known limits. On source inspection,
the judgment's payment sentence does not contain the requested fact groups for
`lt-12`/`am-05`; the notice's modal/action sentence does satisfy `lt-20` facts.
No actual model projection was tested. Do not change the simulated writer just
to make these gates green. Narrowing projection to question-specific claims or
accepting a different golden contract needs a separate owner decision.

`git diff --check`: PASS. No linter/type-check configuration or infrastructure
change applies to these Python-only changes. Relevant authorization/retrieval
and cross-matter security suites are in the 379-test command above.

Changed old pins: `test_evidence` now passes evidence into the renderer and
expects the exact sentence including articles; `test_chat` expects the same
whole source instead of the English template. `phase14_holdout_runner` passes
its selected evidence to the new required renderer argument, keeping its role
reversal protection; its tests passed. Frozen datasets, thresholds and gates,
Public Edge template and historical result reports are unchanged.

No live AWS, model, Harness, IAM, Bedrock score, CloudWatch delivery, OCR/parser
or deployed-path verification was performed. No resources/costs were created.
After an approved release the search Lambda still needs a reviewed **Gateway
change set**; Public Edge code-only CD does not update it. No later phase began.
