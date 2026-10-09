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
- `idp-ocr-corpus-20261009-h.json`: 6 retained records; and
- `idp-ocr-targeted-20261009-i.json`: 2 targeted records that supersede only
  the corresponding failed H attempts.

The consolidated set has 18/18 explicit result records and 17/18 records with
a classification. The remaining record is `judgment-03-en-mixed-interpretive`,
which failed its anchor; its targeted follow-up also failed, so no weak or
alternate anchor was accepted. The classification confusion is:

```text
expected CONTRACT: 5 CONTRACT
expected DEMAND:   5 DEMAND
expected JUDGMENT: 4 JUDGMENT, 1 UNKNOWN
expected UNKNOWN:  3 UNKNOWN
```

Fifteen of 18 records carried a review-required status (83.33%). This is an
observed workflow status, not accuracy, acceptance, or a release gate. The
evaluator reported 32,874 input tokens, 9,721 output tokens, 14 OCR API calls,
and 9 OCR pages over records with usable metadata; one failed record had
unknown usage. Latency remains `UNKNOWN` because the immutable exports use
`latencyMs` while the evaluator's accepted metadata field is `latency_ms`.
No explicit regional pricing file was supplied, so the cost estimate remains
`UNKNOWN`; no rate or zero-cost result is invented.

The same evaluator found these metadata-level errors (not silently converted
into accuracy claims): case-level `ACCEPTANCE_MISMATCH` 17,
`EVIDENCE_MISSING` 3, `FIELD_MISSING` 1, `ORACLE_ANCHOR_MISMATCH` 14,
`ORIGIN_MISMATCH` 10, and `VALUE_DIGEST_INVALID` 9. Field/evidence-level
counts were respectively 59, 6, 7, 33, 20, and 30. These counters include
known contract/export mismatches and require review; they do not authorize
accepting weak anchors.

The extractor `1.0.2` is present across all 18 records, but the newer
classifier prompt `1.0.1` covers only the two targeted follow-ups. This is not
full new-prompt coverage. The historical `COMPLETED` label on review fields in
the digital corpus is preserved as an export-enum erratum and is interpreted
under the corrected review-required contract. The earlier failed exports and
the collection-only OCR metadata remain immutable and are not discarded or
double-counted.
