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
