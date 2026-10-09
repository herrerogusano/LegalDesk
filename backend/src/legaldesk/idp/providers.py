"""Bounded Amazon Bedrock Converse adapters for IDP stages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from ..prompts import FileSystemSystemPromptProvider, SystemPromptArtifact
from .acquisition import normalize_page_text
from .models import DocumentType, IDPContractError
from .processing import ClassificationResult, ExtractionResult, IDPOutputError, parse_classifier_output, parse_extractor_output, parse_strict_json
from .registry import IDPSchema


class IDPProviderError(IDPContractError):
    pass


def default_idp_prompt_path(kind: str) -> Path:
    if kind not in {"classifier", "extractor"}:
        raise IDPProviderError("IDP prompt kind is invalid")
    filename = f"idp-{kind}.md"
    candidates = (
        # Repository checkout: backend/src/legaldesk/idp/providers.py -> the
        # project root is parents[4].  Keep package-local paths below for the
        # release ZIP and Lambda /var/task layout.
        Path(__file__).resolve().parents[4] / "prompts" / filename,
        Path(__file__).resolve().parents[3] / "prompts" / filename,
        Path(__file__).resolve().parents[2] / "prompts" / filename,
        Path("/var/task/prompts") / filename,
    )
    for path in candidates:
        if path.is_file():
            return path
    raise IDPProviderError("IDP prompt artifact is unavailable")


def idp_prompt_identity(label: str = "") -> str:
    classifier = FileSystemSystemPromptProvider(default_idp_prompt_path("classifier")).load()
    extractor = FileSystemSystemPromptProvider(default_idp_prompt_path("extractor")).load()
    prefix = label.strip()
    # The content hash catches same-label edits; the artifact versions and
    # IDs make the persisted identity explainable during recovery and review.
    identity = ":".join(
        (
            classifier.prompt_id,
            classifier.version,
            classifier.sha256,
            extractor.prompt_id,
            extractor.version,
            extractor.sha256,
        )
    )
    return f"{prefix}:{identity}" if prefix else identity


class ConverseClient(Protocol):
    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class IDPModelConfig:
    model_id: str
    max_input_chars: int = 300_000
    classifier_max_tokens: int = 512
    extractor_max_tokens: int = 2_048

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise IDPProviderError("IDP model id is required")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (self.max_input_chars, self.classifier_max_tokens, self.extractor_max_tokens)):
            raise IDPProviderError("IDP model bounds are invalid")
        if self.max_input_chars > 1_000_000 or self.classifier_max_tokens > 4096 or self.extractor_max_tokens > 8192:
            raise IDPProviderError("IDP model bounds exceed the safety ceiling")


def _schema_value(value_type: str) -> dict[str, object]:
    return {
        "string": {"type": "string"},
        "date": {"type": "string"},
        "number": {"type": "number"},
        "boolean": {"type": "boolean"},
        "array[string]": {"type": "array", "items": {"type": "string"}},
    }.get(value_type, {"type": "string"})


def classifier_json_schema() -> dict[str, object]:
    return {
        "type": "object", "additionalProperties": False, "required": ["document_type", "evidence"],
        "properties": {
            "document_type": {"type": "string", "enum": [item.value for item in DocumentType]},
            "subtype": {"type": "string", "enum": ["NDA"]},
            "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["page", "quote"], "properties": {"page": {"type": "integer"}, "quote": {"type": "string"}}}},
        },
    }


def extractor_json_schema(schema: IDPSchema) -> dict[str, object]:
    # A homogeneous array keeps the provider grammar compact.  The selected
    # registry schema remains authoritative: ``field`` is an enum and the
    # adapter below expands these rows into the existing field-name mapping.
    field_entry = {
        "type": "object", "additionalProperties": False,
        "required": ["field", "presence", "value", "reason", "evidence"],
        "properties": {
            "field": {"type": "string", "enum": sorted(schema.fields)},
            "presence": {"type": "string", "enum": ["PRESENT", "ABSENT", "NOT_APPLICABLE", "AMBIGUOUS", "UNKNOWN"]},
            "value": {"anyOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
            "reason": {"type": "string", "description": "Empty when no explanation is needed."},
            "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["page", "quote"], "properties": {"page": {"type": "integer"}, "quote": {"type": "string"}}}},
        },
    }
    return {
        "type": "object", "additionalProperties": False, "required": ["schema_version", "fields"],
        "properties": {"schema_version": {"type": "string"}, "fields": {"type": "array", "items": field_entry}},
    }


def _decode_extractor_wire_output(raw: str | bytes, *, schema: IDPSchema) -> str:
    """Expand compact provider rows into the stable internal parser shape."""

    value = parse_strict_json(raw)
    if not isinstance(value, dict) or set(value) != {"schema_version", "fields"} or not isinstance(value.get("schema_version"), str) or not isinstance(value.get("fields"), list):
        raise IDPOutputError("extractor wire output envelope is invalid")
    rows = value["fields"]
    if len(rows) != len(schema.fields):
        raise IDPOutputError("extractor wire output field coverage is incomplete")
    expected = set(schema.fields)
    seen: set[str] = set()
    expanded: dict[str, object] = {}
    allowed = {"field", "presence", "value", "reason", "evidence"}
    for row in rows:
        if not isinstance(row, dict) or set(row) != allowed:
            raise IDPOutputError("extractor wire field entry is invalid")
        name = row.get("field")
        if not isinstance(name, str) or name not in expected or name in seen:
            raise IDPOutputError("extractor wire field name is invalid")
        seen.add(name)
        expanded[name] = {key: row[key] for key in ("presence", "value", "reason", "evidence")}
    if seen != expected:
        raise IDPOutputError("extractor wire field coverage is incomplete")
    return json.dumps({"schema_version": value["schema_version"], "fields": expanded}, ensure_ascii=False, separators=(",", ":"))


def _output_config(schema: Mapping[str, object], *, name: str) -> dict[str, object]:
    # Keep this to the subset supported by Converse outputConfig; local
    # validation owns all bounds and semantic checks.
    return {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": {"schema": json.dumps(schema, sort_keys=True, separators=(",", ":")), "name": name}}}}


def _input_text(page_text: Mapping[int, str], *, max_chars: int) -> str:
    if not isinstance(page_text, Mapping) or any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in page_text):
        raise IDPProviderError("IDP page input is invalid")
    if any(not isinstance(text, str) for text in page_text.values()):
        raise IDPProviderError("IDP page text is invalid")
    payload = json.dumps({"pages": [{"page": page, "text": normalize_page_text(text)} for page, text in sorted(page_text.items())]}, ensure_ascii=False, separators=(",", ":"))
    if len(payload) > max_chars:
        raise IDPProviderError("IDP model input exceeds the configured bound")
    return payload


def _extractor_input_text(schema: IDPSchema, page_text: Mapping[int, str], *, max_chars: int) -> str:
    """Build the untrusted-data envelope with server-owned field semantics.

    Field meaning and criticality come only from the versioned registry.  Page
    text remains data, never instructions or a source of schema selection.
    """

    if not isinstance(schema, IDPSchema):
        raise IDPProviderError("IDP schema input is invalid")
    fields = [
        {
            "name": name,
            "type": spec.value_type,
            "description": spec.description,
            "evidenceRequired": spec.evidence_required,
            "reviewSensitive": spec.review_sensitive,
            "lexicalKind": spec.lexical_kind,
        }
        for name, spec in sorted(schema.fields.items())
    ]
    payload = json.dumps(
        {"documentType": schema.document_type.value, "schemaVersion": schema.version, "fields": fields,
         "pages": [{"page": page, "text": normalize_page_text(text)} for page, text in sorted(page_text.items())]},
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(payload) > max_chars:
        raise IDPProviderError("IDP model input exceeds the configured bound")
    return payload


def _text_from_response(response: Mapping[str, object]) -> str:
    stop_reason = response.get("stopReason")
    if stop_reason in {"max_tokens", "length"}:
        raise IDPOutputError("IDP model output was truncated")
    try:
        content = response["output"]["message"]["content"]  # type: ignore[index]
        text = next(item["text"] for item in content if isinstance(item, Mapping) and isinstance(item.get("text"), str))
    except (KeyError, IndexError, StopIteration, TypeError) as exc:
        raise IDPOutputError("IDP model returned no text output") from exc
    return text


def _provider_metadata(response: Mapping[str, object]) -> dict[str, object]:
    """Retain bounded provider accounting metadata, never raw response text."""

    usage = response.get("usage")
    metrics = response.get("metrics")
    metadata: dict[str, object] = {}
    if isinstance(usage, Mapping):
        metadata["usage"] = {str(key): int(value) for key, value in usage.items() if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000}
    if isinstance(metrics, Mapping):
        metadata["metrics"] = {str(key): int(value) for key, value in metrics.items() if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000}
    return metadata


def _prompt(path: Path | None, *, kind: str) -> SystemPromptArtifact:
    path = default_idp_prompt_path(kind) if path is None else path
    return FileSystemSystemPromptProvider(path).load()


class IDPConverseClassifier:
    def __init__(self, client: ConverseClient, *, config: IDPModelConfig, prompt_path: Path | None = None) -> None:
        self.client, self.config, self.prompt = client, config, _prompt(prompt_path, kind="classifier")

    def request(self, *, page_text: Mapping[int, str]) -> dict[str, object]:
        return {"modelId": self.config.model_id, "system": [{"text": self.prompt.content}], "messages": [{"role": "user", "content": [{"text": _input_text(page_text, max_chars=self.config.max_input_chars)}]}], "inferenceConfig": {"maxTokens": self.config.classifier_max_tokens, "temperature": 0.0}, "outputConfig": _output_config(classifier_json_schema(), name="legaldesk_idp_classifier")}

    def classify(self, *, page_text: Mapping[int, str], content_sha256: str) -> ClassificationResult:
        result = self.client.converse(**self.request(page_text=page_text))
        return parse_classifier_output(_text_from_response(result), page_text=page_text, content_sha256=content_sha256, prompt_version=f"{self.prompt.prompt_id}:{self.prompt.version}:{self.prompt.sha256}", model_id=self.config.model_id, provider_metadata=_provider_metadata(result))

    def request_hash(self, *, page_text: Mapping[int, str], content_sha256: str) -> str:
        return hashlib.sha256(json.dumps({"stage": "CLASSIFIER", "model": self.config.model_id, "prompt": self.prompt.sha256, "sha": content_sha256, "input": _input_text(page_text, max_chars=self.config.max_input_chars)}, sort_keys=True).encode()).hexdigest()


class IDPConverseExtractor:
    def __init__(self, client: ConverseClient, *, config: IDPModelConfig, prompt_path: Path | None = None) -> None:
        self.client, self.config, self.prompt = client, config, _prompt(prompt_path, kind="extractor")

    def request(self, *, schema: IDPSchema, page_text: Mapping[int, str]) -> dict[str, object]:
        return {"modelId": self.config.model_id, "system": [{"text": self.prompt.content}], "messages": [{"role": "user", "content": [{"text": _extractor_input_text(schema, page_text, max_chars=self.config.max_input_chars)}]}], "inferenceConfig": {"maxTokens": self.config.extractor_max_tokens, "temperature": 0.0}, "outputConfig": _output_config(extractor_json_schema(schema), name="legaldesk_idp_extractor")}

    def extract(self, *, schema: IDPSchema, page_text: Mapping[int, str], content_sha256: str) -> ExtractionResult:
        result = self.client.converse(**self.request(schema=schema, page_text=page_text))
        wire_output = _decode_extractor_wire_output(_text_from_response(result), schema=schema)
        parsed = parse_extractor_output(wire_output, schema=schema, page_text=page_text, content_sha256=content_sha256)
        return ExtractionResult(parsed.document_type, parsed.schema_version, parsed.fields, _provider_metadata(result))

    def request_hash(self, *, schema: IDPSchema, page_text: Mapping[int, str], content_sha256: str) -> str:
        return hashlib.sha256(json.dumps({"stage": "EXTRACTOR", "schema": schema.version, "model": self.config.model_id, "prompt": self.prompt.sha256, "sha": content_sha256, "input": _extractor_input_text(schema, page_text, max_chars=self.config.max_input_chars)}, sort_keys=True).encode()).hexdigest()


def boto3_idp_runtime_client(*, region: str, connect_timeout: int = 5, read_timeout: int = 90) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client("bedrock-runtime", region_name=region, config=Config(connect_timeout=connect_timeout, read_timeout=read_timeout, retries={"total_max_attempts": 1, "mode": "standard"}))


__all__ = ["ConverseClient", "IDPConverseClassifier", "IDPConverseExtractor", "IDPModelConfig", "IDPProviderError", "boto3_idp_runtime_client", "classifier_json_schema", "default_idp_prompt_path", "extractor_json_schema", "idp_prompt_identity"]
