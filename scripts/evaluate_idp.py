"""Offline, metadata-only evaluation report for the IDP fixture corpus.

This module deliberately has no AWS, boto3, network, model, or document-reader
dependency.  It consumes an explicit fixture manifest and an explicit export of
observed results.  It is an evaluator, not a runner: an absent result is
``NOT_EXECUTED`` and missing operational metadata is ``UNKNOWN``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping


MAX_FIXTURES = 25
MAX_INPUT_BYTES = 2 * 1024 * 1024
PROVENANCE_KINDS = {"local", "mock", "real_model", "aws_e2e", "unknown"}
PROVIDER_ERROR_CATEGORIES = {
    "ACCESS_DENIED", "AUTHENTICATION", "INTERNAL", "INVALID_REQUEST", "RATE_LIMITED",
    "SERVICE_UNAVAILABLE", "THROTTLED", "TIMEOUT", "VALIDATION", "UNKNOWN_ERROR",
}
_SHA256 = set("0123456789abcdefABCDEF")


class EvaluationError(ValueError):
    """The explicit evaluation contract is malformed or unsafe."""


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _SHA256 for char in value)


def _digest(value: object) -> str:
    """Digest a value without returning its contents to the report."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean_category(value: object, default: str = "UNKNOWN_ERROR") -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    # Error categories are intentionally a closed allowlist, not sanitized
    # free-form messages.  Sanitizing alone would still leak secret text.
    cleaned = "".join(char if char.isalnum() or char in "_-" else "_" for char in value.strip().upper())
    return cleaned if cleaned in PROVIDER_ERROR_CATEGORIES else default


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError(f"{name} must be an object")
    return value


def _load_json(path: Path, name: str) -> Any:
    if not path.is_file():
        raise EvaluationError(f"{name} file does not exist")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise EvaluationError(f"{name} exceeds the bounded {MAX_INPUT_BYTES}-byte input limit")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"{name} is not valid UTF-8 JSON") from exc


def _expected_field(field: Mapping[str, Any]) -> dict[str, Any]:
    presence = field.get("presence")
    value = field.get("value")
    supplied_digest = field.get("valueDigest", field.get("value_digest"))
    value_digest = supplied_digest.lower() if presence == "PRESENT" and _is_sha256(supplied_digest) else (_digest(value) if presence == "PRESENT" and "value" in field else None)
    expected: dict[str, Any] = {
        "presence": presence if isinstance(presence, str) else "UNKNOWN",
        "value_digest": value_digest,
        "origin": field.get("origin") if isinstance(field.get("origin"), str) else "UNKNOWN",
        "acceptance": field.get("acceptance") if isinstance(field.get("acceptance"), str) else "UNKNOWN",
        "evidence": [],
    }
    for anchor in field.get("evidence", ()) if isinstance(field.get("evidence", ()), list) else ():
        if not isinstance(anchor, Mapping):
            continue
        page = anchor.get("page")
        if isinstance(page, int) and not isinstance(page, bool):
            item: dict[str, Any] = {"page": page}
            if _is_sha256(anchor.get("quoteDigest", anchor.get("quote_digest"))):
                item["quote_digest"] = str(anchor.get("quoteDigest", anchor.get("quote_digest"))).lower()
            elif isinstance(anchor.get("quote"), str):
                item["quote_digest"] = _text_digest(anchor["quote"])
            content_hash = anchor.get("contentSha256", anchor.get("content_sha256"))
            if _is_sha256(content_hash):
                item["content_sha256"] = str(content_hash).lower()
            expected["evidence"].append(item)
    return expected


def load_manifest(path: str | Path, *, max_fixtures: int = MAX_FIXTURES) -> dict[str, Any]:
    """Load and normalize the fixture oracle without retaining document text."""

    raw = _mapping(_load_json(Path(path), "manifest"), "manifest")
    fixtures = raw.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise EvaluationError("manifest.fixtures must be a non-empty list")
    if len(fixtures) > max_fixtures or len(fixtures) > MAX_FIXTURES:
        raise EvaluationError("manifest exceeds the bounded fixture limit")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_fixture in fixtures:
        fixture = _mapping(raw_fixture, "manifest fixture")
        fixture_id = fixture.get("id") or fixture.get("fixtureId")
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            raise EvaluationError("manifest fixture id is required")
        fixture_id = fixture_id.strip()
        if fixture_id in seen:
            raise EvaluationError(f"duplicate manifest fixture id: {fixture_id}")
        seen.add(fixture_id)
        expected = fixture.get("expected") if isinstance(fixture.get("expected"), Mapping) else {}
        fields_raw = expected.get("fields", {}) if isinstance(expected.get("fields", {}), Mapping) else {}
        fields = {str(name): _expected_field(_mapping(value, f"expected field {name}")) for name, value in fields_raw.items()}
        expected_type = fixture.get("expected_type", expected.get("document_type", "UNKNOWN"))
        normalized.append({
            "id": fixture_id,
            "expected_type": expected_type if isinstance(expected_type, str) else "UNKNOWN",
            "expected_sha256": str(fixture.get("sha256")).lower() if _is_sha256(fixture.get("sha256")) else None,
            "page_count": fixture.get("page_count") if isinstance(fixture.get("page_count"), int) and not isinstance(fixture.get("page_count"), bool) else None,
            "fields": fields,
            "derived": expected.get("derived") if isinstance(expected.get("derived"), Mapping) else None,
            # Identity constraints are optional because the published synthetic
            # corpus intentionally has no deployment tenant/matter IDs.
            "identity": {
                key: value
                for key, aliases in {
                    "tenantId": ("tenantId", "tenant_id"),
                    "matterId": ("matterId", "matter_id"),
                    "documentId": ("documentId", "document_id"),
                    "runId": ("runId", "run_id"),
                }.items()
                for alias in aliases
                if isinstance((value := fixture.get(alias)), str)
            },
        })
    return {
        "manifest_version": raw.get("manifest_version") if isinstance(raw.get("manifest_version"), str) else None,
        "dataset_id": raw.get("dataset_id") if isinstance(raw.get("dataset_id"), str) else None,
        "fixtures": normalized,
    }


def _result_records(raw: object) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Accept an explicit ``results`` or persistence-compatible ``runs`` export."""

    envelope = _mapping(raw, "results export")
    records = envelope.get("results", envelope.get("runs"))
    if records is None:
        raise EvaluationError("results export must contain explicit results or runs")
    if not isinstance(records, list):
        raise EvaluationError("results export records must be a list")
    if len(records) > MAX_FIXTURES:
        raise EvaluationError("results export exceeds the bounded fixture limit")
    return envelope, tuple(_mapping(item, "result record") for item in records)


def _provenance(envelope: Mapping[str, Any], records: tuple[Mapping[str, Any], ...]) -> dict[str, Any]:
    envelope_raw = envelope.get("provenance")
    envelope_provenance = envelope_raw if isinstance(envelope_raw, Mapping) else {}
    envelope_kind = envelope_provenance.get("kind", "unknown")
    if envelope_kind not in PROVENANCE_KINDS:
        raise EvaluationError("provenance.kind must be local, mock, real_model, aws_e2e, or unknown")
    record_kinds = {
        record["provenance"].get("kind")
        for record in records
        if isinstance(record.get("provenance"), Mapping) and record["provenance"].get("kind") is not None
    }
    records_with_provenance = sum(1 for record in records if isinstance(record.get("provenance"), Mapping) and record["provenance"].get("kind") is not None)
    if any(kind not in PROVENANCE_KINDS for kind in record_kinds):
        raise EvaluationError("result provenance kind is invalid")
    if envelope_kind == "unknown" and record_kinds and records_with_provenance != len(records):
        raise EvaluationError("explicit and unknown per-record provenance is not countable")
    if len(record_kinds) > 1 or (record_kinds and envelope_kind != "unknown" and record_kinds != {envelope_kind}):
        raise EvaluationError("mixed envelope and per-record provenance is not countable")
    kind = next(iter(record_kinds), envelope_kind)
    raw = envelope_provenance
    observed_real_results = len(records) if kind in {"real_model", "aws_e2e"} else 0
    return {
        "kind": kind,
        "release_sha256": raw.get("release_sha256") if _is_sha256(raw.get("release_sha256")) else None,
        "model_id": raw.get("model_id") if isinstance(raw.get("model_id"), str) else None,
        "prompt_version": raw.get("prompt_version") if isinstance(raw.get("prompt_version"), str) else None,
        "observed_real_results": observed_real_results,
        "counts_as_real_model": bool(observed_real_results and kind in {"real_model", "aws_e2e"}),
    }


def _record_fixture_id(record: Mapping[str, Any]) -> str:
    value = record.get("fixtureId", record.get("fixture_id", record.get("caseId")))
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError("each result requires fixtureId")
    return value.strip()


def _actual_identity(record: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for result_key, fixture_key in (("tenantId", "tenantId"), ("matterId", "matterId"), ("documentId", "documentId"), ("runId", "runId")):
        expected = fixture["identity"].get(fixture_key)
        actual = record.get(result_key)
        if expected is not None and actual != expected:
            errors.append("IDENTITY_MISMATCH")
    return errors


def _actual_digest(field: Mapping[str, Any]) -> str | None:
    digest = field.get("valueDigest", field.get("value_digest"))
    if digest is not None:
        return digest.lower() if _is_sha256(digest) else None
    # Persistence exports contain the value, while reports must not. Hashing
    # it here lets this consumer compare it without emitting it.
    return _digest(field["value"]) if field.get("presence") == "PRESENT" and "value" in field else None


def _actual_anchor(
    anchor: Mapping[str, Any],
    page_count: int | None,
    expected: list[Mapping[str, Any]],
    actual_document_sha256: str | None,
    expected_document_sha256: str | None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    page = anchor.get("page")
    item: dict[str, Any] = {"page": page if isinstance(page, int) and not isinstance(page, bool) else None}
    if not isinstance(page, int) or isinstance(page, bool):
        errors.append("EVIDENCE_PAGE_UNKNOWN")
    elif page < 1 or (page_count is not None and page > page_count):
        errors.append("EVIDENCE_PAGE_OUT_OF_RANGE")
    quote_digest = anchor.get("quoteDigest", anchor.get("quote_digest"))
    if isinstance(quote_digest, str) and _is_sha256(quote_digest):
        item["quote_digest"] = quote_digest.lower()
    elif isinstance(anchor.get("quote"), str):
        item["quote_digest"] = _text_digest(anchor["quote"])
    else:
        errors.append("EVIDENCE_DIGEST_MISSING")
    content_hash = anchor.get("contentSha256", anchor.get("content_sha256"))
    if ("contentSha256" in anchor or "content_sha256" in anchor) and not _is_sha256(content_hash):
        errors.append("EVIDENCE_CONTENT_HASH_INVALID")
    elif _is_sha256(content_hash):
        item["content_sha256"] = str(content_hash).lower()
        if not actual_document_sha256 or not expected_document_sha256:
            errors.append("EVIDENCE_DOCUMENT_HASH_UNKNOWN")
        else:
            if item["content_sha256"] != actual_document_sha256:
                errors.append("EVIDENCE_CONTENT_HASH_MISMATCH")
            if item["content_sha256"] != expected_document_sha256:
                errors.append("EVIDENCE_CONTENT_HASH_NOT_FIXTURE")
    else:
        errors.append("EVIDENCE_CONTENT_HASH_MISSING")
    if item.get("page") is not None and item.get("quote_digest") and expected:
        expected_pairs = {(e.get("page"), e.get("quote_digest")) for e in expected if e.get("quote_digest")}
        if (item.get("page"), item.get("quote_digest")) not in expected_pairs:
            errors.append("ORACLE_ANCHOR_MISMATCH")
        else:
            oracle = next(e for e in expected if (e.get("page"), e.get("quote_digest")) == (item.get("page"), item.get("quote_digest")))
    elif item.get("page") is not None and item.get("quote_digest") and not expected:
        errors.append("ORACLE_ANCHOR_MISMATCH")
    return item, errors


def _field_report(
    name: str,
    expected: Mapping[str, Any],
    actual: Mapping[str, Any] | None,
    page_count: int | None,
    *,
    executed: bool,
    actual_document_sha256: str | None = None,
    expected_document_sha256: str | None = None,
) -> dict[str, Any]:
    if actual is None:
        missing_code = "FIELD_MISSING" if executed else "FIELD_NOT_EXECUTED"
        return {"expected_presence": expected["presence"], "actual_presence": None, "presence_match": "UNKNOWN", "value_match": "UNKNOWN", "origin_match": "UNKNOWN", "acceptance_match": "UNKNOWN", "evidence": {"expected_count": len(expected["evidence"]), "actual_count": None, "valid": None, "errors": [missing_code]}}
    errors: list[str] = []
    if "valueDigest" in actual or "value_digest" in actual:
        supplied_digest = actual.get("valueDigest", actual.get("value_digest"))
        if not _is_sha256(supplied_digest):
            errors.append("VALUE_DIGEST_INVALID")
    actual_presence = actual.get("presence") if isinstance(actual.get("presence"), str) else "UNKNOWN"
    presence_match = "PASS" if actual_presence == expected["presence"] else ("UNKNOWN" if actual_presence == "UNKNOWN" or expected["presence"] == "UNKNOWN" else "FAIL")
    actual_origin = actual.get("origin") if isinstance(actual.get("origin"), str) else "UNKNOWN"
    actual_acceptance = actual.get("acceptance") if isinstance(actual.get("acceptance"), str) else "UNKNOWN"
    origin_match = "PASS" if actual_origin == expected["origin"] else ("UNKNOWN" if actual_origin == "UNKNOWN" or expected["origin"] == "UNKNOWN" else "FAIL")
    acceptance_match = "PASS" if actual_acceptance == expected["acceptance"] else ("UNKNOWN" if actual_acceptance == "UNKNOWN" or expected["acceptance"] == "UNKNOWN" else "FAIL")
    if origin_match == "FAIL":
        errors.append("ORIGIN_MISMATCH")
    if acceptance_match == "FAIL":
        errors.append("ACCEPTANCE_MISMATCH")
    if expected["acceptance"] == "REVIEW_REQUIRED" and actual_acceptance == "AUTO_ACCEPTED":
        errors.append("ACCEPTANCE_UNSAFE_AUTO_ACCEPTED")
    actual_digest = _actual_digest(actual)
    if expected["presence"] == "PRESENT" and actual_presence == "PRESENT" and actual_digest:
        value_match = "PASS" if actual_digest == expected["value_digest"] else "FAIL"
    elif expected["presence"] != "PRESENT" and actual_presence != "PRESENT":
        value_match = "PASS"
    else:
        value_match = "UNKNOWN"
    anchors = actual.get("evidence", ())
    if not isinstance(anchors, list):
        anchors = []
        errors.append("EVIDENCE_NOT_A_LIST")
    actual_anchors: list[dict[str, Any]] = []
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            errors.append("EVIDENCE_MALFORMED")
            continue
        normalized, anchor_errors = _actual_anchor(anchor, page_count, expected["evidence"], actual_document_sha256, expected_document_sha256)
        actual_anchors.append(normalized)
        errors.extend(anchor_errors)
    if expected["presence"] == "PRESENT" and not actual_anchors:
        errors.append("EVIDENCE_MISSING")
    uncertain_errors = {
        "ORACLE_ANCHOR_MISMATCH", "EVIDENCE_CONTENT_HASH_MISSING", "EVIDENCE_DOCUMENT_HASH_UNKNOWN",
        "EVIDENCE_DIGEST_MISSING", "VALUE_DIGEST_INVALID",
    }
    definite_errors = set(errors) - uncertain_errors
    evidence_valid = "PASS" if not errors and (not actual_anchors or expected["evidence"]) else ("FAIL" if definite_errors else "UNKNOWN")
    if actual_digest:
        # Keep only the digest, never the exported value.
        value_digest: str | None = actual_digest
    else:
        value_digest = None
    return {
        "expected_presence": expected["presence"], "actual_presence": actual_presence,
        "presence_match": presence_match, "value_match": value_match, "origin_match": origin_match,
        "acceptance_match": acceptance_match, "actual_origin": actual_origin, "actual_acceptance": actual_acceptance,
        "value_digest": value_digest,
        "evidence": {"expected_count": len(expected["evidence"]), "actual_count": len(actual_anchors), "valid": evidence_valid, "errors": sorted(set(errors))},
    }


def _derived_report(expected: Mapping[str, Any] | None, actual: object) -> dict[str, Any]:
    if expected is None:
        return {"status": "NOT_APPLICABLE"}
    if not isinstance(actual, Mapping):
        return {"status": "UNKNOWN", "errors": ["DERIVED_NOT_EXECUTED"]}
    comparisons: dict[str, str] = {}
    for key, expected_value in expected.items():
        # Free-form explanations and acceptance prose are deliberately not
        # copied into a metadata report; rule/value comparisons use digests.
        if key in {"review_reason", "reason"}:
            continue
        if key not in actual:
            comparisons[str(key)] = "UNKNOWN"
        else:
            comparisons[str(key)] = "PASS" if _digest(actual[key]) == _digest(expected_value) else "FAIL"
    return {"status": "PASS" if comparisons and all(value == "PASS" for value in comparisons.values()) else ("FAIL" if any(value == "FAIL" for value in comparisons.values()) else "UNKNOWN"), "comparisons": comparisons}


def _metric_values(records: list[Mapping[str, Any]], path: tuple[str, ...]) -> dict[str, Any]:
    values: list[float] = []
    unknown = 0
    for record in records:
        current: Any = record
        for key in path:
            current = current.get(key) if isinstance(current, Mapping) else None
        if isinstance(current, (int, float)) and not isinstance(current, bool) and math.isfinite(float(current)) and current >= 0:
            values.append(float(current))
        else:
            unknown += 1
    return {"value": int(sum(values)) if values and all(value.is_integer() for value in values) else (sum(values) if values else None), "observed_records": len(values), "unknown_records": unknown}


def _cost_report(records: list[Mapping[str, Any]], pricing: Mapping[str, Any] | None) -> dict[str, Any]:
    if pricing is None:
        return {"status": "UNKNOWN", "reason": "PRICING_NOT_SUPPLIED", "estimate": None}
    bedrock = pricing.get("bedrock") if isinstance(pricing.get("bedrock"), Mapping) else {}
    textract = pricing.get("textract") if isinstance(pricing.get("textract"), Mapping) else {}
    required = (bedrock.get("input_per_1k_tokens"), bedrock.get("output_per_1k_tokens"), textract.get("per_page"))
    region = pricing.get("region")
    currency = pricing.get("currency")
    if not isinstance(region, str) or not region.strip() or not isinstance(currency, str) or not currency.strip():
        return {"status": "UNKNOWN", "reason": "REGION_OR_CURRENCY_MISSING", "estimate": None}
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0 for value in required):
        return {"status": "UNKNOWN", "reason": "INCOMPLETE_EXPLICIT_PRICING", "estimate": None}
    metrics = {name: _metric_values(records, path) for name, path in {
        "input_tokens": ("observed", "tokens", "input"), "output_tokens": ("observed", "tokens", "output"), "ocr_pages": ("observed", "ocr", "pages"),
    }.items()}
    if any(item["unknown_records"] for item in metrics.values()) or not all(item["observed_records"] for item in metrics.values()):
        return {"status": "UNKNOWN", "reason": "OBSERVED_USAGE_METADATA_INCOMPLETE", "estimate": None, "region": region, "currency": currency}
    estimate = (metrics["input_tokens"]["value"] / 1000) * float(required[0]) + (metrics["output_tokens"]["value"] / 1000) * float(required[1]) + metrics["ocr_pages"]["value"] * float(required[2])
    return {"status": "ESTIMATED", "region": region, "currency": currency, "estimate": round(estimate, 8), "basis": "supplied observed metadata and explicit regional rates"}


def evaluate(manifest: Mapping[str, Any], results: Mapping[str, Any] | None = None, pricing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a metadata-only report from normalized manifest and export data."""

    fixtures = list(manifest.get("fixtures", ()))
    if not fixtures or len(fixtures) > MAX_FIXTURES:
        raise EvaluationError("normalized manifest fixture bound is invalid")
    fixture_by_id = {fixture["id"]: fixture for fixture in fixtures}
    envelope: Mapping[str, Any] = results if isinstance(results, Mapping) else {"provenance": {"kind": "unknown"}, "results": []}
    envelope, records_tuple = _result_records(envelope)
    provenance = _provenance(envelope, records_tuple)
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in records_tuple:
        fixture_id = _record_fixture_id(record)
        if fixture_id not in fixture_by_id:
            raise EvaluationError(f"result references unknown fixture id: {fixture_id}")
        if fixture_id in by_id:
            raise EvaluationError(f"duplicate result for fixture id: {fixture_id}")
        by_id[fixture_id] = record

    cases: list[dict[str, Any]] = []
    confusion: dict[str, dict[str, int]] = {}
    executed = 0
    reviewed = 0
    for fixture in fixtures:
        fixture_id = fixture["id"]
        record = by_id.get(fixture_id)
        expected_type = fixture["expected_type"]
        if record is None:
            actual_type = None
            case = {"fixture_id": fixture_id, "status": "NOT_EXECUTED", "classification": {"expected": expected_type, "actual": None, "match": "UNKNOWN"}, "document_hash": {"expected": fixture["expected_sha256"], "actual": None, "match": "UNKNOWN"}, "fields": {name: _field_report(name, expected, None, fixture["page_count"], executed=False) for name, expected in fixture["fields"].items()}, "derived": _derived_report(fixture["derived"], None), "review": {"required": None, "observed": None, "status": "UNKNOWN"}, "errors": ["NOT_EXECUTED"]}
        else:
            executed += 1
            raw_type = record.get("documentType", record.get("document_type"))
            actual_type = raw_type if isinstance(raw_type, str) and raw_type in {"CONTRACT", "DEMAND", "JUDGMENT", "UNKNOWN"} else None
            identity_errors = _actual_identity(record, fixture)
            classification_match = "PASS" if actual_type == expected_type else ("UNKNOWN" if actual_type is None else "FAIL")
            actual_hash = record.get("documentSha256", record.get("document_sha256"))
            actual_hash = actual_hash.lower() if _is_sha256(actual_hash) else None
            hash_match = "PASS" if fixture["expected_sha256"] and actual_hash == fixture["expected_sha256"] else ("UNKNOWN" if not fixture["expected_sha256"] or not actual_hash else "FAIL")
            actual_fields = record.get("fields", {}) if isinstance(record.get("fields", {}), Mapping) else {}
            fields = {name: _field_report(name, expected, actual_fields.get(name) if isinstance(actual_fields.get(name), Mapping) else None, fixture["page_count"], executed=True, actual_document_sha256=actual_hash, expected_document_sha256=fixture["expected_sha256"]) for name, expected in fixture["fields"].items()}
            errors = list(identity_errors)
            if raw_type is not None and actual_type is None:
                errors.append("CLASSIFICATION_INVALID")
            if classification_match == "FAIL":
                errors.append("CLASSIFICATION_MISMATCH")
            if hash_match == "FAIL":
                errors.append("DOCUMENT_HASH_MISMATCH")
            if ("documentSha256" in record or "document_sha256" in record) and actual_hash is None:
                errors.append("DOCUMENT_HASH_INVALID")
            errors.extend(error for item in fields.values() for error in item["evidence"]["errors"] if error not in {"FIELD_NOT_EXECUTED"})
            raw_status = record.get("status") if isinstance(record.get("status"), str) else "UNKNOWN"
            observed_status = raw_status if raw_status in {"COMPLETED", "REVIEW_REQUIRED", "IDP_REVIEW_REQUIRED", "FAILED", "PROCESSING", "WAITING_FOR_OCR", "UNKNOWN"} else "UNKNOWN"
            review_required = observed_status in {"REVIEW_REQUIRED", "IDP_REVIEW_REQUIRED"} or any(
                item.get("actual_presence") is not None
                and isinstance(actual_fields.get(name), Mapping)
                and actual_fields[name].get("acceptance") == "REVIEW_REQUIRED"
                for name, item in fields.items()
            )
            reviewed += int(review_required)
            case = {"fixture_id": fixture_id, "status": "OBSERVED", "observed_status": observed_status, "classification": {"expected": expected_type, "actual": actual_type, "match": classification_match}, "document_hash": {"expected": fixture["expected_sha256"], "actual": actual_hash, "match": hash_match}, "fields": fields, "derived": _derived_report(fixture["derived"], record.get("derived")), "review": {"required": review_required, "observed": observed_status, "status": "OBSERVED"}, "errors": sorted(set(errors))}
        if case["status"] != "NOT_EXECUTED":
            actual_for_confusion = case["classification"]["actual"] or "UNKNOWN"
            row = confusion.setdefault(expected_type, {})
            row[actual_for_confusion] = row.get(actual_for_confusion, 0) + 1
        cases.append(case)

    report_records = list(by_id.values())
    metrics = {"input_tokens": _metric_values(report_records, ("observed", "tokens", "input")), "output_tokens": _metric_values(report_records, ("observed", "tokens", "output")), "ocr_pages": _metric_values(report_records, ("observed", "ocr", "pages")), "ocr_api_calls": _metric_values(report_records, ("observed", "ocr", "api_calls")), "latency_ms": _metric_values(report_records, ("observed", "latency_ms"))}
    provider_errors: dict[str, int] = {}
    for record in report_records:
        errors = record.get("observed", {}).get("providerErrors", ()) if isinstance(record.get("observed"), Mapping) else ()
        if isinstance(errors, list):
            for error in errors:
                category = _clean_category(error.get("category") if isinstance(error, Mapping) else error)
                provider_errors[category] = provider_errors.get(category, 0) + 1
    return {
        "report_schema_version": "1.0",
        "dataset": {"manifest_version": manifest.get("manifest_version"), "dataset_id": manifest.get("dataset_id"), "fixture_count": len(fixtures)},
        "provenance": provenance,
        "counts": {"fixtures": len(fixtures), "executed": executed, "not_executed": len(fixtures) - executed, "review_required": reviewed},
        "classification_confusion": confusion,
        "review_rate": {"status": "UNKNOWN" if executed == 0 else "OBSERVED", "reviewed": reviewed if executed else None, "denominator": executed if executed else None, "rate": reviewed / executed if executed else None},
        "observed_metrics": metrics,
        "provider_errors": provider_errors,
        "cost": _cost_report(report_records, pricing),
        "cases": cases,
    }


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--results", type=Path, help="Explicit result export; omit for an offline NOT_EXECUTED report")
    parser.add_argument("--pricing", type=Path, help="Explicit regional pricing JSON; never inferred")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        results = _load_json(args.results, "results") if args.results else {"provenance": {"kind": "unknown"}, "results": []}
        pricing = _mapping(_load_json(args.pricing, "pricing"), "pricing") if args.pricing else None
        report = evaluate(manifest, results, pricing)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except (EvaluationError, OSError) as exc:
        print(f"evaluate_idp: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI smoke test
    raise SystemExit(_cli())
