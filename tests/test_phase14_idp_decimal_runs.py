"""Runs read from DynamoDB must leave persistence as JSON-native numbers.

boto3 returns every stored number as ``Decimal``.  The in-memory repository
keeps plain ints, so only a Decimal-shaped item exercises the real boundary
that broke ``get_document_metadata`` for the first promoted live run.
"""

from __future__ import annotations

import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from legaldesk.idp.persistence import _run_from_item  # noqa: E402


def _boto3_shaped_run_item() -> dict[str, object]:
    return {
        "runId": "run-1", "tenantId": "tenant", "matterId": "matter", "documentId": "doc",
        "documentSha256": "a" * 64, "documentType": "CONTRACT", "schemaVersion": "1.0",
        "modelId": "model", "promptVersion": "1.0.2", "status": "IDP_REVIEW_REQUIRED",
        "createdAt": "2026-10-13T12:31:51+00:00",
        "fields": {
            "amount": {
                "field": "amount", "value": Decimal("1250"), "presence": "PRESENT", "origin": "LITERAL",
                "acceptance": "REVIEW_REQUIRED",
                "evidence": [{"page": Decimal("2"), "quote": "1.250 EUR", "contentSha256": "b" * 64, "start": Decimal("10"), "end": Decimal("19")}],
            },
            "notice_days": {
                "field": "notice_days", "value": Decimal("30.5"), "presence": "PRESENT", "origin": "INTERPRETIVE",
                "acceptance": "PROVISIONAL", "evidence": [],
                "provenance": {"parameters": {"duration_value": Decimal("12")}},
            },
        },
    }


class DecimalRunBoundaryTests(unittest.TestCase):
    def test_numbers_are_json_native_after_reading_a_boto3_item(self) -> None:
        run = _run_from_item(_boto3_shaped_run_item())
        amount, notice = run.fields["amount"], run.fields["notice_days"]
        self.assertEqual((amount.value, type(amount.value)), (1250, int))
        self.assertEqual((notice.value, type(notice.value)), (30.5, float))
        self.assertEqual(notice.provenance["parameters"]["duration_value"], 12)
        json.dumps({name: {"value": field.value, "validation": field.validation, "provenance": field.provenance} for name, field in run.fields.items()}, default=dict)  # read-only mappings only; Decimal still fails

    def test_non_finite_stored_number_is_rejected(self) -> None:
        item = _boto3_shaped_run_item()
        item["fields"]["amount"]["value"] = Decimal("NaN")  # type: ignore[index]
        with self.assertRaises(Exception):
            _run_from_item(item)


if __name__ == "__main__":
    unittest.main()
