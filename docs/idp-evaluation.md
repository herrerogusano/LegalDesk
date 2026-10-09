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
