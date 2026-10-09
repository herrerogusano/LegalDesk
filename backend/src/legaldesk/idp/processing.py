"""Provider-neutral IDP classification, extraction and paid-stage contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from .acquisition import normalize_page_text
from .models import (
    DocumentType,
    EvidenceAnchor,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    IDPContractError,
    IDPFieldResult,
    IDP_SCHEMA_VERSION,
    utc_now,
)
from .registry import IDPSchema


MAX_MODEL_OUTPUT_BYTES = 400_000
MAX_EVIDENCE_ANCHORS = 8
MAX_EVIDENCE_QUOTE_CHARS = 4_000
MAX_STAGE_KEY_CHARS = 128


class IDPOutputError(IDPContractError):
    """A model/provider output failed the server-owned contract."""


class EvidenceValidationError(IDPOutputError):
    """A claimed anchor is not present in canonical page text."""


def _reject_constant(value: str) -> None:
    raise IDPOutputError(f"non-finite JSON number: {value}")


def _duplicate_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IDPOutputError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_strict_json(raw: str | bytes, *, max_bytes: int = MAX_MODEL_OUTPUT_BYTES) -> Any:
    """Parse model JSON without duplicate keys, NaN/Infinity, or oversize output."""

    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise IDPOutputError("model output exceeds the bounded size")
        try:
            raw = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise IDPOutputError("model output is not UTF-8") from exc
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > max_bytes:
        raise IDPOutputError("model output exceeds the bounded size")
    try:
        return json.loads(raw, object_pairs_hook=_duplicate_key, parse_constant=_reject_constant)
    except IDPOutputError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise IDPOutputError("model output is not complete strict JSON") from exc


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IDPOutputError(f"{label} must be an object")
    return value


def _only_keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise IDPOutputError(f"{label} contains unknown fields: {sorted(unknown)}")


def validate_evidence_anchor(
    raw: Mapping[str, Any],
    *,
    page_text: Mapping[int, str],
    content_sha256: str,
) -> EvidenceAnchor:
    """Verify a model quote against server-normalized page text."""

    _only_keys(raw, {"page", "quote", "start", "end", "content_sha256"}, "evidence")
    page = raw.get("page")
    quote = raw.get("quote")
    supplied_hash = raw.get("content_sha256", content_sha256)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise EvidenceValidationError("evidence page is invalid")
    if not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_EVIDENCE_QUOTE_CHARS:
        raise EvidenceValidationError("evidence quote is invalid")
    if supplied_hash != content_sha256:
        raise EvidenceValidationError("evidence content hash is not server-owned")
    canonical = page_text.get(page)
    if canonical is None:
        raise EvidenceValidationError("evidence page is outside the document")
    canonical_normalized = normalize_page_text(canonical).casefold()
    quote_normalized = normalize_page_text(quote).casefold()
    offset = canonical_normalized.find(quote_normalized) if quote_normalized else -1
    if offset < 0:
        raise EvidenceValidationError("evidence quote is not present on the claimed page")
    start = raw.get("start")
    end = raw.get("end")
    if start is not None and (isinstance(start, bool) or not isinstance(start, int) or start < 0):
        raise EvidenceValidationError("evidence start is invalid")
    if end is not None and (isinstance(end, bool) or not isinstance(end, int) or end < 0):
        raise EvidenceValidationError("evidence end is invalid")
    derived_end = offset + len(quote_normalized)
    # Model-supplied spans are advisory only.  Accept them only when they
    # exactly match the server-normalized text; otherwise return server-owned
    # offsets rather than preserving a fabricated location.
    if start is not None and start != offset:
        raise EvidenceValidationError("evidence start does not match canonical page text")
    if end is not None and end != derived_end:
        raise EvidenceValidationError("evidence end does not match canonical page text")
    return EvidenceAnchor(page, quote, content_sha256, offset, derived_end)


def _evidence(
    raw: Any, *, page_text: Mapping[int, str], content_sha256: str, required: bool = True
) -> tuple[EvidenceAnchor, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_EVIDENCE_ANCHORS:
        raise EvidenceValidationError("evidence must be a bounded list")
    anchors = tuple(
        validate_evidence_anchor(_object(item, "evidence"), page_text=page_text, content_sha256=content_sha256)
        for item in raw
    )
    if required and not anchors:
        raise EvidenceValidationError("a present field requires evidence")
    return anchors


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    document_type: DocumentType
    subtype: str | None
    evidence: tuple[EvidenceAnchor, ...]
    prompt_version: str
    model_id: str
    acceptance: FieldAcceptance


def parse_classifier_output(
    raw: str | bytes,
    *,
    page_text: Mapping[int, str],
    content_sha256: str,
    prompt_version: str,
    model_id: str,
) -> ClassificationResult:
    value = _object(parse_strict_json(raw), "classifier output")
    _only_keys(value, {"document_type", "subtype", "evidence"}, "classifier output")
    try:
        document_type = DocumentType(value.get("document_type"))
    except (ValueError, TypeError):
        raise IDPOutputError("classifier returned an unsupported document type") from None
    subtype = value.get("subtype")
    if subtype is not None:
        if not isinstance(subtype, str) or subtype.strip().upper() != "NDA" or document_type is not DocumentType.CONTRACT:
            raise IDPOutputError("classifier subtype is unsupported")
        subtype = "NDA"
    evidence = _evidence(value.get("evidence", []), page_text=page_text, content_sha256=content_sha256, required=False)
    # Classification acceptance is server policy; model output cannot mark it human-confirmed.
    acceptance = FieldAcceptance.AUTO_ACCEPTED if evidence else FieldAcceptance.PROVISIONAL
    return ClassificationResult(document_type, subtype, evidence, prompt_version, model_id, acceptance)


class DocumentClassifier(Protocol):
    def classify(self, *, page_text: Mapping[int, str], content_sha256: str) -> ClassificationResult: ...


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    document_type: DocumentType
    schema_version: str
    fields: Mapping[str, IDPFieldResult]


def _validate_value(value: Any, value_type: str) -> bool:
    if value_type == "string":
        return isinstance(value, str) and bool(value.strip())
    if value_type == "date":
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value)
            return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        except ValueError:
            return False
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "array[string]":
        return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item.strip() for item in value)
    return False


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def _normalized_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
        return text
    # Only unambiguous day/month-name/year and month-name/day/year forms are
    # accepted. Numeric 01/02/2026 is deliberately not guessed.
    match = re.fullmatch(r"(\d{1,2})\s+(?:de\s+)?([A-Za-záéíóúñ]+)\s+(?:de\s+)?(\d{4})", text, re.IGNORECASE)
    if match is None:
        match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text, re.IGNORECASE)
        if match is None:
            return None
        month, day, year = _MONTHS.get(match.group(1).lower()), match.group(2), match.group(3)
    else:
        day, month, year = match.group(1), _MONTHS.get(match.group(2).lower()), match.group(3)
    if month is None:
        return None
    try:
        parsed = datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d")


def _numeric_tokens(text: str) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for token in re.findall(r"(?<![\w])\d[\d.,]*(?![\w])", text):
        candidate = token.replace(" ", "")
        if candidate.count(",") and candidate.count("."):
            candidate = candidate.replace(",", "") if candidate.rfind(".") > candidate.rfind(",") else candidate.replace(".", "").replace(",", ".")
        elif candidate.count(",") == 1 and len(candidate.rsplit(",", 1)[1]) <= 2:
            candidate = candidate.replace(",", ".")
        else:
            candidate = candidate.replace(",", "")
        try:
            values.append(Decimal(candidate))
        except InvalidOperation:
            continue
    return tuple(values)


def _lexical_support(value: Any, value_type: str, field_name: str, anchors: tuple[EvidenceAnchor, ...], page_text: Mapping[int, str]) -> bool:
    """Return true only when the quote lexically supports a literal value."""

    if not anchors:
        return False
    quoted = " ".join(anchor.quote for anchor in anchors).casefold()
    if value_type == "date":
        normalized = _normalized_date(value)
        if normalized is None:
            return False
        date_tokens = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:de\s+)?[A-Za-záéíóúñ]+\s+(?:de\s+)?\d{4}|[A-Za-z]+\s+\d{1,2},?\s+\d{4}", quoted)
        return normalized in (date_value for date_value in (_normalized_date(token) for token in date_tokens) if date_value)
    if value_type == "number" and field_name in {"amount", "claimed_amount", "initial_duration_value", "renewal_period_value", "termination_notice_value"}:
        try:
            expected = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return False
        return any(candidate == expected for candidate in _numeric_tokens(quoted))
    if value_type == "number":
        return True
    if value_type == "string":
        return normalize_page_text(str(value)).casefold() in normalize_page_text(quoted).casefold()
    if value_type == "array[string]":
        return all(normalize_page_text(str(item)).casefold() in normalize_page_text(quoted).casefold() for item in value)
    return False


def _unavailable_field(name: str, schema_version: str, reason: str) -> IDPFieldResult:
    return IDPFieldResult(name, presence=FieldPresence.UNKNOWN, acceptance=FieldAcceptance.UNAVAILABLE, schema_version=schema_version, reason=reason)


def parse_extractor_output(
    raw: str | bytes,
    *,
    schema: IDPSchema,
    page_text: Mapping[int, str],
    content_sha256: str,
) -> ExtractionResult:
    value = _object(parse_strict_json(raw), "extractor output")
    _only_keys(value, {"schema_version", "fields"}, "extractor output")
    if value.get("schema_version", schema.version) != schema.version:
        raise IDPOutputError("extractor schema version does not match the server-selected schema")
    raw_fields = _object(value.get("fields"), "extractor fields")
    _only_keys(raw_fields, set(schema.fields), "extractor fields")
    result: dict[str, IDPFieldResult] = {}
    for name, spec in schema.fields.items():
        item = raw_fields.get(name)
        if item is None:
            result[name] = _unavailable_field(name, schema.version, "field not provided; no review required")
            continue
        if not isinstance(item, dict):
            result[name] = _unavailable_field(name, schema.version, "field record is malformed")
            continue
        _only_keys(item, {"value", "presence", "evidence", "reason"}, f"field {name}")
        try:
            presence = FieldPresence(item.get("presence", "UNKNOWN"))
            field_value = item.get("value")
            if presence is FieldPresence.PRESENT:
                if spec.value_type == "date":
                    field_value = _normalized_date(field_value)
                if not _validate_value(field_value, spec.value_type):
                    raise IDPOutputError("value does not match the registry type or an unambiguous format")
                anchors = _evidence(item.get("evidence", []), page_text=page_text, content_sha256=content_sha256, required=spec.evidence_required)
                lexically_supported = _lexical_support(field_value, spec.value_type, name, anchors, page_text)
                # Review-sensitive values and values whose quote cannot prove
                # the literal token remain provisional, never auto-accepted.
                acceptance = FieldAcceptance.AUTO_ACCEPTED if spec.review_sensitive is False and lexically_supported else FieldAcceptance.PROVISIONAL
                origin = FieldOrigin.LITERAL if lexically_supported and not spec.review_sensitive else FieldOrigin.INTERPRETIVE
            else:
                if field_value is not None:
                    raise IDPOutputError("non-present fields cannot contain a value")
                anchors = _evidence(item.get("evidence", []), page_text=page_text, content_sha256=content_sha256, required=False)
                acceptance = FieldAcceptance.PROVISIONAL if presence is FieldPresence.AMBIGUOUS else FieldAcceptance.UNAVAILABLE
                origin = FieldOrigin.INTERPRETIVE if presence is FieldPresence.AMBIGUOUS else FieldOrigin.LITERAL
            result[name] = IDPFieldResult(name, field_value, presence, origin, acceptance, anchors, reason=item.get("reason"))
        except EvidenceValidationError:
            result[name] = _unavailable_field(name, schema.version, "evidence validation failed")
        except IDPOutputError:
            result[name] = _unavailable_field(name, schema.version, "field validation failed")
    return ExtractionResult(schema.document_type, schema.version, result)


class DocumentFieldExtractor(Protocol):
    def extract(self, *, schema: IDPSchema, page_text: Mapping[int, str], content_sha256: str) -> ExtractionResult: ...


class PaidStage(StrEnum):
    CLASSIFIER = "CLASSIFIER"
    EXTRACTOR = "EXTRACTOR"
    OCR = "OCR"


class PaidStageState(StrEnum):
    READY = "READY"
    IN_FLIGHT = "IN_FLIGHT"
    COMMITTED = "COMMITTED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class PaidStageRecord:
    run_id: str
    stage: PaidStage
    request_hash: str
    state: PaidStageState
    lease_token: str | None = None
    lease_until: datetime | None = None
    artifact_ref: str | None = None


class StageCallLedger:
    """Atomic local contract mirroring the conditional durable stage ledger."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, PaidStage], PaidStageRecord] = {}
        self._lock = threading.Lock()

    def begin(self, *, run_id: str, stage: PaidStage, request_hash: str, lease_seconds: int = 300) -> PaidStageRecord:
        if not run_id or not request_hash or len(request_hash) > MAX_STAGE_KEY_CHARS:
            raise IDPContractError("stage key is invalid")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 900:
            raise IDPContractError("stage lease is invalid")
        now = utc_now()
        with self._lock:
            existing = self._records.get((run_id, stage))
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IDPContractError("stage request configuration changed for the run")
                if existing.state is PaidStageState.COMMITTED and existing.artifact_ref:
                    return existing
                if existing.state is PaidStageState.IN_FLIGHT:
                    raise IDPContractError("stage paid call is already in flight")
                if existing.state is PaidStageState.AMBIGUOUS:
                    raise IDPContractError("stage paid call has an ambiguous outcome")
            record = PaidStageRecord(run_id, stage, request_hash, PaidStageState.IN_FLIGHT, uuid4().hex, now + timedelta(seconds=lease_seconds))
            self._records[(run_id, stage)] = record
            return record

    def commit(self, record: PaidStageRecord, *, artifact_ref: str) -> PaidStageRecord:
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise IDPContractError("committed stage requires a durable artifact reference")
        with self._lock:
            current = self._records.get((record.run_id, record.stage))
            if current != record or current.state is not PaidStageState.IN_FLIGHT:
                raise IDPContractError("stage lease is stale or already committed")
            if current.lease_until is None or current.lease_until <= utc_now():
                raise IDPContractError("stage lease has expired")
            updated = PaidStageRecord(record.run_id, record.stage, record.request_hash, PaidStageState.COMMITTED, artifact_ref=artifact_ref)
            self._records[(record.run_id, record.stage)] = updated
            return updated

    def mark_ambiguous(self, record: PaidStageRecord) -> PaidStageRecord:
        with self._lock:
            current = self._records.get((record.run_id, record.stage))
            if current != record or current.state is not PaidStageState.IN_FLIGHT:
                raise IDPContractError("stage lease is stale or no longer in flight")
            updated = PaidStageRecord(record.run_id, record.stage, record.request_hash, PaidStageState.AMBIGUOUS)
            self._records[(record.run_id, record.stage)] = updated
            return updated

    def get(self, *, run_id: str, stage: PaidStage) -> PaidStageRecord | None:
        with self._lock:
            return self._records.get((run_id, stage))


__all__ = [
    "ClassificationResult",
    "DocumentClassifier",
    "DocumentFieldExtractor",
    "EvidenceValidationError",
    "ExtractionResult",
    "IDPOutputError",
    "MAX_EVIDENCE_ANCHORS",
    "MAX_EVIDENCE_QUOTE_CHARS",
    "PaidStage",
    "PaidStageRecord",
    "PaidStageState",
    "StageCallLedger",
    "parse_classifier_output",
    "parse_extractor_output",
    "parse_strict_json",
    "validate_evidence_anchor",
]
