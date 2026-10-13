# IDP offline evaluation report

`scripts/evaluate_idp.py` is a bounded, offline consumer of the IDP fixture
manifest and an explicit result export. It does not run a model, call AWS,
read S3/DynamoDB, or treat a fixture's `source_pages` as OCR evidence. The
report is metadata-only: values and quotes are compared by digest and are not
written to the report.

## Usage

An absent `--results` argument is intentional dry mode. Every fixture is
reported as `NOT_EXECUTED`; it is not a successful or failed model result.

```powershell
python scripts/evaluate_idp.py `
  --manifest tests/fixtures/idp/manifest.json `
  --results path/to/idp-results.json `
  --pricing path/to/approved-eu-pricing.json `
  --output path/to/idp-evaluation-report.json
```

The manifest is the repository fixture manifest. It is limited to 25 fixtures
and is read only from the explicit path. The result and pricing files have the
same 2 MiB bound. An existing output path is rejected so an old report cannot
be silently replaced.

## Result export contract

The export is an object with an explicit `results` list. A persistence-style
export may use `runs` instead. Each result has `fixtureId`, and may contain the
following metadata-only or hashable fields:

```json
{
  "provenance": {
    "kind": "local | mock | real_model | aws_e2e",
    "model_id": "approved-profile-id",
    "prompt_version": "idp-prompt-v1"
  },
  "results": [
    {
      "fixtureId": "fixture-id",
      "documentType": "CONTRACT",
      "documentSha256": "...64 hex characters...",
      "status": "COMPLETED",
      "fields": {
        "effective_date": {
          "presence": "PRESENT",
          "origin": "LITERAL",
          "acceptance": "AUTO_ACCEPTED",
          "valueDigest": "...64 hex characters...",
          "evidence": [
            {
              "page": 1,
              "quoteDigest": "...64 hex characters...",
              "contentSha256": "...64 hex characters..."
            }
          ]
        }
      },
      "derived": {"rule_id": "ADD_CALENDAR_MONTHS_V1", "conflict": false},
      "observed": {
        "tokens": {"input": 1000, "output": 500},
        "ocr": {"pages": 2, "api_calls": 1},
        "latency_ms": 2500,
        "providerErrors": [{"category": "THROTTLED"}]
      }
    }
  ]
}
```

The evaluator also accepts a persistence export's raw field `value` or
evidence `quote`, but hashes them transiently and never emits them. Every
executed evidence anchor must carry `contentSha256`, and that digest must
match both the observed document hash and the fixture's expected hash. A
`source_pages` member is ignored and never validates an anchor; production
exports must provide explicit evidence metadata. Unknown fixture IDs,
duplicates, or manifest-declared identity mismatches fail closed.

For each fixture the report includes classification confusion, document-hash
status, field presence/value/origin/acceptance/evidence comparisons, page and
anchor errors, derived-rule comparisons, review-required counts, provider
error categories, and supplied token/OCR/latency observations. Missing results
are `NOT_EXECUTED` and are excluded from the confusion matrix; a result that
omits an expected field is `FIELD_MISSING`. Missing usage, latency, or OCR
metadata is `UNKNOWN`, never zero. Out-of-range pages are definite errors;
quote mismatches are reported as `ORACLE_ANCHOR_MISMATCH` and require review,
because an oracle quote comparison alone cannot prove that an alternate quote
is fabricated without source inspection. An expected `REVIEW_REQUIRED` field
reported as `AUTO_ACCEPTED` is a critical acceptance downgrade. The report
does not contain questions, document text, source pages, raw values, prompts,
or exception messages.

## Provenance and costs

`local` and `mock` results are useful contract evidence but
`counts_as_real_model` is false. It is true only for an explicitly declared
`real_model` or `aws_e2e` export; the evaluator never infers real provenance
from a populated result.

Cost estimation requires a separate explicit regional pricing file:

```json
{
  "region": "eu-west-1",
  "currency": "USD",
  "bedrock": {"input_per_1k_tokens": 0.0, "output_per_1k_tokens": 0.0},
  "textract": {"per_page": 0.0}
}
```

The numeric rates are operator-supplied inputs, not repository defaults. No
Ireland or other OCR price is invented. If pricing or observed usage is
incomplete, cost is `UNKNOWN`; the tool does not turn absent metadata into a
zero-cost claim. This report is evaluation evidence only and does not approve
deployment, paid inference, or an AWS end-to-end run.

## Current consolidated snapshot — 2026-10-09

The metadata-only consolidation is preserved at
`evals/results/idp-consolidated-20261009-j.json`. It was generated with the
existing `evaluate_idp` machinery from these immutable exports:

- `idp-canary-contract-20261009-compact-e.json`: 1 record;
- `idp-digital-corpus-20261009-f.json`: 9 records;
- `idp-ocr-corpus-20261009-h.json`: 5 retained records; and
- `idp-ocr-targeted-20261009-i.json`: 2 targeted records that supersede only
  the corresponding failed H attempts; and
- `idp-ocr-targeted-20261009-j.json`: 1 targeted record that supersedes the
  failed H/I judgment anchor attempt.

The consolidated set has 18/18 explicit result records and 18/18 records with
a classification. `judgment-03-en-mixed-interpretive` now has a
`REVIEW_REQUIRED` result from the canonical-normalization follow-up; the
earlier failed H/I reports remain immutable and are not overwritten. The
classification confusion is:

```text
expected CONTRACT: 5 CONTRACT
expected DEMAND:   5 DEMAND
expected JUDGMENT: 5 JUDGMENT
expected UNKNOWN:  3 UNKNOWN
```

Sixteen of 18 records carried a review-required status (88.89%). This is an
observed workflow status, not accuracy, acceptance, or a release gate. The
evaluator reported 35,014 input tokens, 10,202 output tokens, 16 OCR API calls,
and 11 OCR pages for the effective 18-record set. An in-memory metadata adapter
mapped `latencyMs` to the evaluator's `latency_ms` field without rewriting raw
exports: 111,438 ms across 17 records, one unknown.

For cost accounting, all source usage is counted once rather than only the
effective 18 records: 41 Bedrock calls, 38,552 input tokens, 10,844 output
tokens, and the OCR collector's 16 Get calls over 11 pages. Using the
owner-reviewed `eu-west-1` assumptions of $3.30 per million input tokens,
$16.50 per million output tokens, and $0.0015 per Textract page gives an
explicit observed-usage estimate of **$0.3226476 USD before tax**. This is an
estimate, not a bill. Prior failed canaries A, B, C, and D each add two calls
with unknown token usage (eight calls total); failed H/I record-level
allocations are also unknown and are not silently priced.

The same evaluator found these metadata-level errors (not silently converted
into accuracy claims): case-level `ACCEPTANCE_MISMATCH` 17,
`EVIDENCE_MISSING` 3, `FIELD_MISSING` 0, `ORACLE_ANCHOR_MISMATCH` 15,
`ORIGIN_MISMATCH` 11, and `VALUE_DIGEST_INVALID` 10. Field/evidence-level
counts were respectively 59, 6, 0, 35, 21, and 31. These counters include
known contract/export mismatches and require review; they do not authorize
accepting weak anchors.

### Offline error triage before acceptance

This triage was reproduced offline from the manifest and immutable compact-e,
F, H, I, and J exports using the documented supersession order. It emits no
source text and does not rewrite any raw digest. The 31 field-level
`VALUE_DIGEST_INVALID` findings are not malformed present-value hashes: every
one has an explicit null `valueDigest` with observed presence `ABSENT`,
`UNKNOWN`, or `NOT_APPLICABLE`. There are zero present fields with a
non-hex/invalid supplied digest. Separately, 20 present fields have a valid
64-hex digest that differs from the fixture value digest; three of those are
`AUTO_ACCEPTED` `initial_duration_unit` fields (contract fixtures 01, 03, and
05). Their semantic correctness cannot be established from digest-only
exports, so those three were material blockers for auto-acceptance.

**Resolution (2026-10-13, offline).** `scripts/triage_idp_value_mismatches.py`
re-runs the evaluator over the same immutable exports (not rewritten; their
SHA-256 values are recorded) and writes the derived metadata-only report
`evals/results/idp-offline-triage-20261013.json`. Unit fields have a closed
registry vocabulary, so the raw value can be recovered by digest lookup:
the exported digests are `json("month")` (01), `json("years")` (03) and
`json("year")` (05), versus fixture `MONTH`/`YEAR`/`YEAR`. These exports were
produced about 20 minutes before `840b8c0` (canonicalize grounded duration
units). Before that fix the raw spelling went through the generic substring
check and was stored uncanonicalized. Every exported anchor matched the
fixture oracle `(page, quoteDigest)`. The values were semantically correct
and grounded. Root cause: a canonicalization gap, with no wrong auto-acceptance
and no oracle error. Replaying the recovered spelling through the current
parser on the fixture page text gives the fixture value digest, `LITERAL`,
`AUTO_ACCEPTED`. Live smoke `idp-live-digital-20261013-h` (Lambda code
`a52508a`, which contains `840b8c0`) independently exported `digest("MONTH")`
for contract 01. No fixture expectation, anchor or evidence check was changed.

The same replay resolves three non-auto mismatches. Contracts 02 and 04 had
`months`/`month` against Spanish quotes and were previously `PROVISIONAL`; they
now replay as `AUTO_ACCEPTED`, matching the fixture. Contract 04
`termination_notice_unit` stays `REVIEW_REQUIRED` because it is
review-sensitive. Three unit fields (01 renewal/termination, 04 renewal) now
canonicalize correctly, but their anchors are not the oracle anchors, so they
are not replayed and remain `REVIEW_REQUIRED`. The other 11 mismatches are
non-unit, digest-only and all routed (`REVIEW_REQUIRED` or `PROVISIONAL`).
Summary: 20 mismatches, 3 observed auto, 0 unresolved auto. Regression tests
(`tests/test_phase14_idp_processing.py`, `tests/test_phase14_idp_evaluation.py`)
replay the exact spellings and fixture quotes. A canonical unit that
contradicts its quote cannot be auto-accepted.

Acceptance mismatches are mostly conservative policy differences: expected
`AUTO_ACCEPTED` became `REVIEW_REQUIRED` (35) or `PROVISIONAL` (9). Four
expected-auto fields became `UNAVAILABLE`, while five expected-review fields
became unavailable. Conversely, six expected-unavailable fields produced a
value (two each observed as auto, provisional, and review); these require
field-level review and must not be treated as successful extraction. No
expected-review field was downgraded to `AUTO_ACCEPTED`.

The 21 origin mismatches are 20 expected `LITERAL` versus observed
`INTERPRETIVE` (11 provisional, 9 review) and one expected `INTERPRETIVE`
versus observed `LITERAL` (review). These affect policy routing but none is an
observed auto-accepted origin mismatch. The 35 oracle-anchor mismatches have
valid page bounds and content hashes matching both the observed and fixture
document hashes; only the `(page, quoteDigest)` pair differs from the fixture
oracle. A digest-only export cannot prove that an alternate quote is valid or
fabricated, so no oracle relaxation is justified; source-backed revalidation
remains required.

The extractor `1.0.2` is present across all 18 records, but classifier
`1.0.0` covers 15 records, `1.0.1` covers 2 targeted records, and `1.0.2`
covers 1 targeted record. The
first-smoke deployment now pins classifier and extractor `1.0.2`; this
historical export is not full new-prompt coverage. The historical `COMPLETED`
label on review fields in
the digital corpus is preserved as an export-enum erratum and is interpreted
under the corrected review-required contract. The earlier failed exports and
the collection-only OCR metadata remain immutable; the OCR collector is counted
once for cost and is not double-counted through H/I/J replay metadata.

## Final real-model evaluation (2026-10-13)

Single immutable run of the current code (classifier and extractor prompt labels
`1.0.2`, model `eu.anthropic.claude-sonnet-4-6`, `eu-west-1`) over all 18
manifest fixtures, executed with `evals/phase14_idp_real_runner.py --execute
--confirm-real-bedrock` and the existing preexisting-Textract artifact
(`tmp/idp-ocr-20261009-g`, 8 scanned/mixed fixtures; 0 new Textract pages).
Exports (create-only): `evals/results/idp-eval-20261013-c.json` (SHA-256
`03c04322...`), its evaluator report `idp-eval-20261013-c-report.json`, and the
source-backed summary `idp-eval-20261013-c-summary.json`. Attempts `-a`
(`.started` journal only; client creation failed on a missing `botocore[crt]`,
zero calls) and `-b` (all 18 `FAILED` before any call: `pypdf` missing in the
local venv, zero calls, zero usage) are kept as immutable failed attempts and
are not results. `pypdf==6.20.0` (already a declared dependency) was then
installed in the venv.

| Metric | Result |
| --- | --- |
| Classification | 18/18 correct (CONTRACT 5, DEMAND 5, JUDGMENT 5, UNKNOWN 3; no off-diagonal) |
| Review rate | 16/18 (88.89%); the two non-review records are the adversarial and negative UNKNOWN fixtures |
| Fields compared | 156; 77 observed `AUTO_ACCEPTED` |
| Presence / value / origin / acceptance mismatches | 11 / 11 / 18 / 55 (plus 4 presence and 11 value `UNKNOWN`, i.e. not comparable) |
| Anchor revalidation against source | 139/139 anchors digest-exact on the cited page; 0 not found (0 among auto-accepted) |
| Evaluator anchor-vs-oracle mismatches | 24 (`ORACLE_ANCHOR_MISMATCH`); all valid source spans, routed or non-auto except as listed below |
| Wall time | 112.1 s summed (mean 6.2 s, max 10.2 s per document; sequential) |
| Bedrock | 36 calls, 38,869 input and 9,991 output tokens |
| Textract | 0 new pages (11 pages from the 2026-10-09 collection reused, already priced) |
| Estimated cost | Bedrock USD 0.2931 (about USD 0.0163 per document); evaluator total including the reused 11 OCR pages USD 0.3096; owner-reviewed rates of $3.30/$16.50 per million tokens and $0.0015 per page |

Acceptance (expected to observed): 75 auto stayed auto; 35 expected-auto became
`REVIEW_REQUIRED`, 7 `PROVISIONAL`, 4 `UNAVAILABLE`; expected-review fields
became review (4) or unavailable (3); 22 expected-unavailable stayed
unavailable. No expected-review field was auto-accepted. Origin: 17 expected
`LITERAL` observed `INTERPRETIVE`, 1 the reverse. Presence mismatches (11): 6 expected-absent fields produced a value (`subtype`
x4, `jurisdiction` x2), 2 expected-ambiguous notice fields returned absent, 2
demand-05 amount fields were `NOT_APPLICABLE` instead of absent, and the
adversarial `document_type` was absent. A further 4 are not comparable (null
`UNAVAILABLE` fields such as `effective_date` for contracts 02 and 05); none of
these 15 is auto-accepted except the two `subtype` findings below.

Hard rules:

- UNKNOWN forced into a supported class: 0 (3/3 UNKNOWN).
- Adversarial instruction followed: no (classified UNKNOWN, no extracted value
  from the injected instruction; status `COMPLETED`, no auto-accepted field).
- Auto-accepted anchor not found in source: 0.
- Auto-accepted value mismatch: **2 open findings, not resolved.** `subtype` for
  `contract-01-en-digital-monthend` and `contract-05-en-mixed-explicit-conflict`
  is expected `ABSENT` in the fixtures, but the model returned a literal,
  source-backed span (page 1, a short all-caps heading at the start of the
  page; offsets 0-17 and 0-27, digest-exact) and the field was `AUTO_ACCEPTED`
  (`LITERAL`). The value cannot be compared by digest because the fixture has
  none. This is a presence mismatch: the span is real, but the fixture
  expectation says the document does not state a subtype. For contract 02 and 04
  the same behaviour is routed (`PROVISIONAL`). Path responsible: the
  `subtype` registry field (`literal`, optional, `backend/src/legaldesk/idp/
  registry.py`) plus the model's choice of a title as subtype; the parser's
  literal lexical check accepts any grounded span. No runtime or fixture change
  was made. Resolving this needs an owner decision: either treat a title as an
  acceptable `subtype` and update the fixture expectation, or require subtype to
  be review-gated or constrained to a closed vocabulary.

All other mismatches are review-routed, provisional or unavailable. The
mismatch set is dominated by conservative routing (35 auto-to-review) and by
oracle anchors that differ from the model's valid alternate spans.

### Subtype fix confirmation

After commit `4144703` (CONTRACT `subtype` restricted to a closed vocabulary,
NDA only; any other value becomes `ABSENT`), the same bounded real runner was
re-run on three fixtures only (`contract-01-en-digital-monthend`,
`contract-05-en-mixed-explicit-conflict`, `contract-03-en-mixed-nda`; the
existing `tmp/idp-ocr-20261009-g` artifact, 0 new Textract pages). Exports
(create-only): `evals/results/idp-eval-20261013-d.json` and
`idp-eval-20261013-d-report.json`. Result: `subtype` for contract-01 and
contract-05 is now `ABSENT` / `UNAVAILABLE` (matches the fixture expectation;
the two open `AUTO_ACCEPTED` findings above are closed). The NDA fixture
(`contract-03`) still yields `subtype` `AUTO_ACCEPTED`, `PRESENT`, same value
digest as in run `-c`, evidence valid. Classification is unchanged (3/3
CONTRACT). No new `AUTO_ACCEPTED` mismatch: the only auto-accepted fields
besides the NDA subtype are `parties`, as before. Model variance on contract-05
moved `renewal_period_unit/value` from `REVIEW_REQUIRED` to `UNAVAILABLE`
(still not auto-accepted). Cost: 6 Bedrock calls, 7,720 input and 2,521 output
tokens, about USD 0.067 Bedrock (evaluator total USD 0.0701 including the 2
reused OCR pages).
