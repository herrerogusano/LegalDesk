"""Contracts of the read-only agentic chat (ADR-021, Phase 15).

Three things are defined here and nowhere else:

* the input schemas of the two new tools (``search_documents`` and
  ``get_extracted_fields``) and the parsers for their results;
* the *answer plan* the agent returns at the end of its loop (ADR-021 D3).
  The plan carries ids, references, dates and numbers from closed vocabularies
  and **no free text**; the backend writes every sentence from
  ``agent_templates``;
* the semantics of IDP fields for the agent (accepted = fact, provisional =
  pending review, absent/rejected = empty).

This module is pure: it does not import Gateway, Bedrock or DynamoDB code, so
the MCP Lambda can use the schemas without pulling in the chat pipeline.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping

from . import agent_templates
from .idp.models import DocumentType, FieldAcceptance, FieldPresence
from .idp.registry import IDPSchemaRegistry


LIST_MATTER_DOCUMENTS = "list_matter_documents"
SEARCH_DOCUMENTS = "search_documents"
GET_EXTRACTED_FIELDS = "get_extracted_fields"
SEARCH_DOCUMENTS_TARGET = "search-documents"

OPAQUE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_OPAQUE_ID = re.compile(OPAQUE_ID_PATTERN)
# Mirrors ``retrieval.MAX_QUERY_LENGTH`` (asserted equal in the tests) so this
# module stays free of the retrieval imports.
MAX_SEARCH_QUESTION_LENGTH = 2_000
MAX_DOCUMENTS_PER_CALL = 20
MAX_FIELD_NAMES_PER_CALL = 16
MAX_RESULT_CITATIONS = 32
MAX_RESULT_PASSAGE_CHARS = 4_000
MAX_RESULT_ANSWER_CHARS = 8_000
MAX_EVIDENCE_PER_FIELD = 2
MAX_EVIDENCE_QUOTE_CHARS = 500
MAX_PLAN_BYTES = 16 * 1024
MAX_PLAN_BLOCKS = 4
# A plan names the source of a text or inventory block by the tool and the position of the call among the calls of that tool in the
# loop (``{"tool": "search_documents", "call": 1}``), not by the tool-use id. The model cannot copy that id: in the T9 step 12 runs
# every one of its attempts (14 of 14 in the second run) named an id that matched no call, while the data blocks, which point at
# document ids the model can read in the tool result, worked. Upper bound = what a loop of 3 iterations can possibly hold.
MAX_SOURCE_CALL = 16
MAX_INVENTORY_DOCUMENTS = 200
MAX_GROUPS_PER_BLOCK = 8
MAX_REFS_PER_GROUP = 100
MAX_AGGREGATES_PER_BLOCK = 6
PLAN_VERSION = 1


class AgentContractError(ValueError):
    """A tool result or answer plan violates its contract.

    ``code`` is a closed, non-sensitive vocabulary suitable for metadata.
    """

    CODES = frozenset({
        "invalid_json", "too_large", "invalid_shape", "unsupported_version",
        "unknown_phrase", "unknown_criterion", "unknown_field",
        "invalid_parameter", "limit_exceeded", "invalid_result",
    })

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("error code is not in the closed vocabulary")
        self.code = code
        super().__init__(code)


def _supported_field_names() -> tuple[str, ...]:
    registry = IDPSchemaRegistry()
    names: set[str] = set()
    for document_type in registry.supported_types():
        names.update(registry.get(document_type).fields)
    return tuple(sorted(names))


IDP_FIELD_NAMES = _supported_field_names()


# --------------------------------------------------------------------- tools

SEARCH_DOCUMENTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "matterId": {
            "type": "string", "pattern": OPAQUE_ID_PATTERN, "maxLength": 128,
            "description": "Matter selector required by the Gateway; the server ignores it for authorization.",
        },
        "question": {
            "type": "string", "minLength": 1, "maxLength": MAX_SEARCH_QUESTION_LENGTH,
            "description": "Question about what the matter's documents say.",
        },
    },
    "required": ["matterId", "question"],
}

GET_EXTRACTED_FIELDS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "matterId": {
            "type": "string", "pattern": OPAQUE_ID_PATTERN, "maxLength": 128,
            "description": "Matter selector required by the Gateway; the server ignores it for authorization.",
        },
        "documentIds": {
            "type": "array", "minItems": 1, "maxItems": MAX_DOCUMENTS_PER_CALL,
            "uniqueItems": True,
            "items": {"type": "string", "pattern": OPAQUE_ID_PATTERN, "maxLength": 128},
            "description": "Documents of the matter, at most 20 per call.",
        },
        "fieldNames": {
            "type": "array", "minItems": 1, "maxItems": MAX_FIELD_NAMES_PER_CALL,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(IDP_FIELD_NAMES)},
            "description": "IDP registry fields to return; omit to get every field the document has.",
        },
    },
    "required": ["matterId", "documentIds"],
}


def parse_get_extracted_fields_arguments(arguments: Mapping[str, object]) -> tuple[tuple[str, ...], tuple[str, ...] | None]:
    """Validate ``get_extracted_fields`` arguments (``matterId`` already removed).

    Returns ``(document_ids, field_names_or_None)``.
    """

    if not isinstance(arguments, Mapping) or set(arguments) - {"documentIds", "fieldNames"} or "documentIds" not in arguments:
        raise AgentContractError("invalid_shape")
    raw_ids = arguments["documentIds"]
    if not isinstance(raw_ids, (list, tuple)) or not 1 <= len(raw_ids) <= MAX_DOCUMENTS_PER_CALL:
        raise AgentContractError("limit_exceeded")
    if any(not isinstance(item, str) or _OPAQUE_ID.fullmatch(item) is None for item in raw_ids) or len(set(raw_ids)) != len(raw_ids):
        raise AgentContractError("invalid_parameter")
    names: tuple[str, ...] | None = None
    if "fieldNames" in arguments:
        raw_names = arguments["fieldNames"]
        if not isinstance(raw_names, (list, tuple)) or not 1 <= len(raw_names) <= MAX_FIELD_NAMES_PER_CALL:
            raise AgentContractError("limit_exceeded")
        if any(not isinstance(item, str) or item not in IDP_FIELD_NAMES for item in raw_names) or len(set(raw_names)) != len(raw_names):
            raise AgentContractError("unknown_field")
        names = tuple(raw_names)
    return tuple(raw_ids), names


# -------------------------------------------------------- IDP field semantics

class FieldStatus(StrEnum):
    FACT = "fact"
    PENDING_REVIEW = "pending_review"
    EMPTY = "empty"


_FACT_ACCEPTANCE = frozenset({FieldAcceptance.AUTO_ACCEPTED.value, FieldAcceptance.HUMAN_CONFIRMED.value})
_PENDING_ACCEPTANCE = frozenset({FieldAcceptance.PROVISIONAL.value, FieldAcceptance.REVIEW_REQUIRED.value})


def classify_field(*, presence: object, acceptance: object, has_value: bool) -> FieldStatus:
    """ADR-021 D3: accepted and auto-accepted are facts, provisional values are
    pending human review, and anything absent, rejected or unavailable is empty.
    """

    if not has_value or presence != FieldPresence.PRESENT.value or not isinstance(acceptance, str):
        return FieldStatus.EMPTY
    if acceptance in _FACT_ACCEPTANCE:
        return FieldStatus.FACT
    if acceptance in _PENDING_ACCEPTANCE:
        return FieldStatus.PENDING_REVIEW
    return FieldStatus.EMPTY


# ------------------------------------------------------------- tool results

@dataclass(frozen=True, slots=True)
class SearchCitation:
    citation_id: str
    document_id: str
    document_name: str | None
    page_number: int | None
    section: str | None
    passage: str


@dataclass(frozen=True, slots=True)
class SearchDocumentsResult:
    """A validated ``search_documents`` result; ``answer`` is shown verbatim."""

    answer: str
    evidence_status: str
    citations: tuple[SearchCitation, ...]


def _str_or_none(value: object, *, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise AgentContractError("invalid_result")
    return value


def parse_search_documents_result(payload: object) -> SearchDocumentsResult:
    """Strictly parse the result of a successful ``search_documents`` call."""

    expected = {"answer", "evidenceStatus", "operationStatus", "citations"}
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise AgentContractError("invalid_result")
    answer = payload["answer"]
    status = payload["evidenceStatus"]
    raw_citations = payload["citations"]
    if (
        payload["operationStatus"] != "ok"
        or not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_RESULT_ANSWER_CHARS
        or not isinstance(status, str) or status not in {"answerable", "ambiguous", "insufficient_evidence"}
        or not isinstance(raw_citations, (list, tuple)) or len(raw_citations) > MAX_RESULT_CITATIONS
    ):
        raise AgentContractError("invalid_result")
    citations: list[SearchCitation] = []
    for raw in raw_citations:
        if not isinstance(raw, Mapping) or set(raw) != {"citationId", "documentId", "documentName", "pageNumber", "section", "passage"}:
            raise AgentContractError("invalid_result")
        citation_id, document_id, passage = raw["citationId"], raw["documentId"], raw["passage"]
        page = raw["pageNumber"]
        if (
            not isinstance(citation_id, str) or _OPAQUE_ID.fullmatch(citation_id) is None
            or not isinstance(document_id, str) or _OPAQUE_ID.fullmatch(document_id) is None
            or not isinstance(passage, str) or not passage.strip() or len(passage) > MAX_RESULT_PASSAGE_CHARS
            or (page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 1))
        ):
            raise AgentContractError("invalid_result")
        citations.append(SearchCitation(
            citation_id=citation_id, document_id=document_id,
            document_name=_str_or_none(raw["documentName"], limit=512),
            page_number=page, section=_str_or_none(raw["section"], limit=512), passage=passage,
        ))
    if len({item.citation_id for item in citations}) != len(citations):
        raise AgentContractError("invalid_result")
    if status != "insufficient_evidence" and not citations:
        raise AgentContractError("invalid_result")
    return SearchDocumentsResult(answer=answer.strip(), evidence_status=status, citations=tuple(citations))


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    page: int
    quote: str


@dataclass(frozen=True, slots=True)
class ExtractedField:
    name: str
    status: FieldStatus
    value: object | None
    origin: str | None
    evidence: tuple[EvidenceAnchor, ...]


@dataclass(frozen=True, slots=True)
class DocumentFields:
    document_id: str
    document_name: str
    document_type: str | None
    fields: Mapping[str, ExtractedField]


@dataclass(frozen=True, slots=True)
class ExtractedFieldsResult:
    documents: Mapping[str, DocumentFields] = field(default_factory=dict)


def _json_value_ok(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, int):
        return abs(value) < 10**15
    if isinstance(value, float):
        return math.isfinite(value) and abs(value) < 10**15
    if isinstance(value, str):
        return len(value) <= 2_000
    if isinstance(value, list):
        return len(value) <= 64 and all(isinstance(item, str) and len(item) <= 500 for item in value)
    return False


def parse_extracted_fields_result(payload: object) -> ExtractedFieldsResult:
    """Strictly parse a ``get_extracted_fields`` payload (see ``mcp_server``)."""

    if not isinstance(payload, Mapping) or set(payload) != {"documents", "notFound"}:
        raise AgentContractError("invalid_result")
    raw_documents, not_found = payload["documents"], payload["notFound"]
    if (
        not isinstance(raw_documents, (list, tuple)) or len(raw_documents) > MAX_DOCUMENTS_PER_CALL
        or not isinstance(not_found, (list, tuple)) or len(not_found) > MAX_DOCUMENTS_PER_CALL
        or any(not isinstance(item, str) for item in not_found)
    ):
        raise AgentContractError("invalid_result")
    documents: dict[str, DocumentFields] = {}
    for raw in raw_documents:
        if not isinstance(raw, Mapping) or set(raw) != {"documentId", "name", "documentType", "fields"}:
            raise AgentContractError("invalid_result")
        document_id, name, doc_type, raw_fields = raw["documentId"], raw["name"], raw["documentType"], raw["fields"]
        if (
            not isinstance(document_id, str) or _OPAQUE_ID.fullmatch(document_id) is None or document_id in documents
            or not isinstance(name, str) or not name.strip() or len(name) > 512
            or (doc_type is not None and (not isinstance(doc_type, str) or doc_type not in {t.value for t in DocumentType}))
            or not isinstance(raw_fields, (list, tuple)) or len(raw_fields) > len(IDP_FIELD_NAMES)
        ):
            raise AgentContractError("invalid_result")
        fields: dict[str, ExtractedField] = {}
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping) or set(raw_field) != {"name", "status", "value", "origin", "evidence"}:
                raise AgentContractError("invalid_result")
            field_name, raw_status = raw_field["name"], raw_field["status"]
            if not isinstance(field_name, str) or field_name not in IDP_FIELD_NAMES or field_name in fields:
                raise AgentContractError("unknown_field")
            if not isinstance(raw_status, str) or raw_status not in {item.value for item in FieldStatus}:
                raise AgentContractError("invalid_result")
            status = FieldStatus(raw_status)
            value, origin = raw_field["value"], raw_field["origin"]
            raw_evidence = raw_field["evidence"]
            if (
                not _json_value_ok(value)
                or (origin is not None and (not isinstance(origin, str) or len(origin) > 32))
                or not isinstance(raw_evidence, (list, tuple)) or len(raw_evidence) > MAX_EVIDENCE_PER_FIELD
                or (status is FieldStatus.EMPTY and (value is not None or raw_evidence))
                or (status is not FieldStatus.EMPTY and value is None)
            ):
                raise AgentContractError("invalid_result")
            anchors: list[EvidenceAnchor] = []
            for item in raw_evidence:
                if not isinstance(item, Mapping) or set(item) != {"page", "quote"}:
                    raise AgentContractError("invalid_result")
                page, quote = item["page"], item["quote"]
                if (
                    isinstance(page, bool) or not isinstance(page, int) or page < 1
                    or not isinstance(quote, str) or not quote.strip() or len(quote) > MAX_EVIDENCE_QUOTE_CHARS
                ):
                    raise AgentContractError("invalid_result")
                anchors.append(EvidenceAnchor(page, quote))
            fields[field_name] = ExtractedField(field_name, status, value, origin, tuple(anchors))
        documents[document_id] = DocumentFields(document_id, name.strip(), doc_type, fields)
    return ExtractedFieldsResult(documents)


# ----------------------------------------------------- inventory (listing)

_DOCUMENT_STATUSES = ("PENDING_UPLOAD", "UPLOADED", "PENDING_INGESTION", "INDEXED", "FAILED")
# A status this version does not list is kept (rendered with a generic label) rather than
# invalidating the whole listing; only its shape is checked.
_STATUS_SHAPE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")


@dataclass(frozen=True, slots=True)
class InventoryDocument:
    document_id: str
    name: str
    media_type: str
    status: str
    uploaded_at: str


@dataclass(frozen=True, slots=True)
class InventoryResult:
    """A validated list_matter_documents result (name, file type, lifecycle status only)."""

    documents: tuple[InventoryDocument, ...]


def parse_list_matter_documents_result(payload: object) -> InventoryResult:
    """Strictly parse the MCP listing; keys the inventory does not use are ignored."""

    raw_documents = payload.get("documents") if isinstance(payload, Mapping) else None
    if not isinstance(raw_documents, (list, tuple)) or len(raw_documents) > MAX_INVENTORY_DOCUMENTS:
        raise AgentContractError("invalid_result")
    documents: list[InventoryDocument] = []
    for raw in raw_documents:
        if not isinstance(raw, Mapping):
            raise AgentContractError("invalid_result")
        document_id, name, media, status, uploaded = (
            raw.get("documentId"), raw.get("name"), raw.get("mediaType"), raw.get("status"), raw.get("uploadedAt"),
        )
        if (
            not isinstance(document_id, str) or _OPAQUE_ID.fullmatch(document_id) is None
            or not isinstance(name, str) or not name.strip() or len(name) > 512
            or not isinstance(media, str) or len(media) > 128
            or not isinstance(status, str) or _STATUS_SHAPE.fullmatch(status) is None
            or not isinstance(uploaded, str) or len(uploaded) > 64
        ):
            raise AgentContractError("invalid_result")
        documents.append(InventoryDocument(document_id, name.strip(), media, status, uploaded))
    if len({item.document_id for item in documents}) != len(documents):
        raise AgentContractError("invalid_result")
    return InventoryResult(tuple(documents))


# ------------------------------------------------------------- answer plan

INTRODUCTION_IDS = tuple(agent_templates.INTRODUCTIONS)
TRANSITION_IDS = tuple(agent_templates.TRANSITIONS)
AGGREGATE_TYPES = ("count", "min", "max")
ORDER_BY = ("document_name", "field_value")
ORDER_DIRECTIONS = ("asc", "desc")
INVENTORY_ORDER_BY = ("document_name", "uploaded_at")
INVENTORY_STATUS_FILTERS = _DOCUMENT_STATUSES


@dataclass(frozen=True, slots=True)
class CriterionSpec:
    id: str
    field: str | None
    params: tuple[tuple[str, str], ...] = ()  # (name, kind) with kind in date|number|document_type
    required: tuple[str, ...] = ()
    at_least_one: bool = False


CRITERIA: Mapping[str, CriterionSpec] = {
    spec.id: spec
    for spec in (
        CriterionSpec("all_documents", None),
        CriterionSpec("expires_in_period", "explicit_expiration_date", (("from", "date"), ("to", "date")), at_least_one=True),
        CriterionSpec("starts_in_period", "effective_date", (("from", "date"), ("to", "date")), at_least_one=True),
        CriterionSpec("automatic_renewal", "automatic_renewal"),
        CriterionSpec("no_automatic_renewal", "automatic_renewal"),
        CriterionSpec("amount_in_range", "amount", (("min", "number"), ("max", "number")), at_least_one=True),
        CriterionSpec("document_type_is", None, (("documentType", "document_type"),), required=("documentType",)),
    )
}
_PLAN_DOCUMENT_TYPES = tuple(t.value for t in DocumentType if t is not DocumentType.UNKNOWN)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    params: Mapping[str, object]

    @property
    def title_key(self) -> str:
        if self.id == "document_type_is":
            return f"document_type_is.{self.params['documentType']}"
        bounds = [name for name in ("from", "to", "min", "max") if name in self.params]
        if self.id in {"expires_in_period", "starts_in_period"}:
            return f"{self.id}.{'both' if len(bounds) == 2 else bounds[0]}"
        if self.id == "amount_in_range":
            return f"{self.id}.{'both' if len(bounds) == 2 else bounds[0]}"
        return self.id


@dataclass(frozen=True, slots=True)
class FieldRef:
    document_id: str
    field: str


@dataclass(frozen=True, slots=True)
class Group:
    criterion: Criterion
    refs: tuple[FieldRef, ...]


@dataclass(frozen=True, slots=True)
class Order:
    by: str
    field: str | None
    direction: str


@dataclass(frozen=True, slots=True)
class AggregateRequest:
    type: str
    field: str | None


@dataclass(frozen=True, slots=True)
class DataBlock:
    groups: tuple[Group, ...]
    order: Order | None = None
    aggregates: tuple[AggregateRequest, ...] = ()
    transition_before: str | None = None


@dataclass(frozen=True, slots=True)
class TextBlock:
    """The verbatim result of one ``search_documents`` call, named by ``tool_use_id`` (exact id) or by ``call`` (its position)."""

    tool_use_id: str
    transition_before: str | None = None
    call: int | None = None


@dataclass(frozen=True, slots=True)
class InventoryBlock:
    """The agent picks a list_matter_documents call, a status filter and an order.

    The backend writes the lines; nothing the agent supplies is ever printed.
    """

    tool_use_id: str
    statuses: tuple[str, ...] = ()
    order_by: str | None = None
    direction: str = "asc"
    transition_before: str | None = None
    call: int | None = None


@dataclass(frozen=True, slots=True)
class AnswerPlan:
    intro: str | None
    blocks: tuple[DataBlock | TextBlock | InventoryBlock, ...]


def _keys(value: object, *, required: set[str], optional: set[str] = frozenset()) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not required <= set(value) or set(value) - required - optional:
        raise AgentContractError("invalid_shape")
    return value


def _opaque(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise AgentContractError("invalid_parameter")
    return value


def _field_name(value: object) -> str:
    if not isinstance(value, str) or value not in IDP_FIELD_NAMES:
        raise AgentContractError("unknown_field")
    return value


def _phrase(value: object, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AgentContractError("unknown_phrase")
    return value


def _parse_param(kind: str, value: object) -> object:
    if kind == "date":
        if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
            raise AgentContractError("invalid_parameter")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise AgentContractError("invalid_parameter") from exc
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AgentContractError("invalid_parameter")
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise AgentContractError("invalid_parameter") from exc
        if not number.is_finite() or abs(number) >= Decimal(10) ** 15:
            raise AgentContractError("invalid_parameter")
        return number
    if kind == "document_type":
        if not isinstance(value, str) or value not in _PLAN_DOCUMENT_TYPES:
            raise AgentContractError("invalid_parameter")
        return value
    raise AgentContractError("invalid_shape")  # pragma: no cover


def _parse_criterion(raw: object) -> Criterion:
    node = _keys(raw, required={"id"}, optional={"params"})
    criterion_id = node["id"]
    if not isinstance(criterion_id, str) or criterion_id not in CRITERIA:
        raise AgentContractError("unknown_criterion")
    spec = CRITERIA[criterion_id]
    raw_params = node.get("params", {})
    if not isinstance(raw_params, Mapping):
        raise AgentContractError("invalid_shape")
    allowed = dict(spec.params)
    if set(raw_params) - set(allowed):
        raise AgentContractError("invalid_shape")
    if not set(spec.required) <= set(raw_params) or (spec.at_least_one and not raw_params):
        raise AgentContractError("invalid_parameter")
    params = {name: _parse_param(allowed[name], value) for name, value in raw_params.items()}
    for low, high in (("from", "to"), ("min", "max")):
        if low in params and high in params and params[low] > params[high]:  # type: ignore[operator]
            raise AgentContractError("invalid_parameter")
    return Criterion(criterion_id, params)


def _parse_data_block(node: Mapping[str, Any]) -> DataBlock:
    groups_raw = node["groups"]
    if not isinstance(groups_raw, (list, tuple)) or not 1 <= len(groups_raw) <= MAX_GROUPS_PER_BLOCK:
        raise AgentContractError("limit_exceeded")
    groups: list[Group] = []
    for raw_group in groups_raw:
        group = _keys(raw_group, required={"criterion", "refs"})
        refs_raw = group["refs"]
        if not isinstance(refs_raw, (list, tuple)) or not 1 <= len(refs_raw) <= MAX_REFS_PER_GROUP:
            raise AgentContractError("limit_exceeded")
        refs = tuple(
            FieldRef(_opaque(_keys(item, required={"documentId", "field"})["documentId"]), _field_name(item["field"]))
            for item in refs_raw
        )
        if len(set(refs)) != len(refs):
            raise AgentContractError("invalid_parameter")
        groups.append(Group(_parse_criterion(group["criterion"]), refs))
    order = None
    if "order" in node:
        raw_order = _keys(node["order"], required={"by", "direction"}, optional={"field"})
        by = _phrase(raw_order["by"], ORDER_BY)
        if (by == "field_value") != ("field" in raw_order):
            raise AgentContractError("invalid_parameter")
        order = Order(by, _field_name(raw_order["field"]) if "field" in raw_order else None,
                      _phrase(raw_order["direction"], ORDER_DIRECTIONS))
    aggregates: list[AggregateRequest] = []
    if "aggregates" in node:
        raw_aggregates = node["aggregates"]
        if not isinstance(raw_aggregates, (list, tuple)) or len(raw_aggregates) > MAX_AGGREGATES_PER_BLOCK:
            raise AgentContractError("limit_exceeded")
        for raw in raw_aggregates:
            item = _keys(raw, required={"type"}, optional={"field"})
            kind = _phrase(item["type"], AGGREGATE_TYPES)
            if kind in {"min", "max"} and "field" not in item:
                raise AgentContractError("invalid_parameter")
            aggregates.append(AggregateRequest(kind, _field_name(item["field"]) if "field" in item else None))
        if len(set(aggregates)) != len(aggregates):
            raise AgentContractError("invalid_parameter")
    return DataBlock(tuple(groups), order, tuple(aggregates), None)


def _parse_source(raw: object, tool: str) -> tuple[str, int | None]:
    """``(tool_use_id, call)`` of a block source: exactly one of the two forms, validated closed.

    * ``{"tool": <the tool of this block type>, "call": n}``: the documented form, n a plain integer from 1;
    * ``{"toolUseId": <id>}``: an exact id (accepted, no longer advertised in the prompt: the model cannot copy it).
    """

    if isinstance(raw, Mapping) and set(raw) == {"toolUseId"}:
        return _opaque(raw["toolUseId"]), None
    source = _keys(raw, required={"tool", "call"})
    call = source["call"]
    if source["tool"] != tool or isinstance(call, bool) or not isinstance(call, int) or not 1 <= call <= MAX_SOURCE_CALL:
        raise AgentContractError("invalid_parameter")
    return "", call


def _parse_inventory_block(node: Mapping[str, Any], tool_use_id: str, transition: str | None, call: int | None = None) -> InventoryBlock:
    statuses: tuple[str, ...] = ()
    if "statusIs" in node:
        raw = node["statusIs"]
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= len(INVENTORY_STATUS_FILTERS):
            raise AgentContractError("limit_exceeded")
        statuses = tuple(_phrase(item, INVENTORY_STATUS_FILTERS) for item in raw)
        if len(set(statuses)) != len(statuses):
            raise AgentContractError("invalid_parameter")
    order_by, direction = None, "asc"
    if "order" in node:
        raw_order = _keys(node["order"], required={"by", "direction"})
        order_by = _phrase(raw_order["by"], INVENTORY_ORDER_BY)
        direction = _phrase(raw_order["direction"], ORDER_DIRECTIONS)
    return InventoryBlock(tool_use_id, statuses, order_by, direction, transition, call)


def _json_loads_strict(text: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_name: str) -> object:
        raise ValueError("non-finite number")

    return json.loads(text, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)


def parse_answer_plan(raw: str | bytes | Mapping[str, object]) -> AnswerPlan:
    """Parse the agent's final output into an :class:`AnswerPlan`.

    Anything outside the contract raises :class:`AgentContractError`: unknown
    keys, duplicate JSON keys, ids outside the closed lists, free text. Whether
    the references exist and the criteria hold is checked by the validator
    against the tool results of the same loop.
    """

    if isinstance(raw, (str, bytes)):
        if len(raw) > MAX_PLAN_BYTES:
            raise AgentContractError("too_large")
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            text = text.strip()
            if text.startswith("```") and text.endswith("```") and "\n" in text:
                # Tolerate exactly one markdown fence around the JSON object.
                text = text[text.index("\n") + 1:-3]
            plan_node = _json_loads_strict(text.strip())
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise AgentContractError("invalid_json") from exc
    else:
        plan_node = raw
    node = _keys(plan_node, required={"version", "blocks"}, optional={"intro"})
    if isinstance(node["version"], bool) or node["version"] != PLAN_VERSION:
        raise AgentContractError("unsupported_version")
    intro = _phrase(node["intro"], INTRODUCTION_IDS) if "intro" in node else None
    blocks_raw = node["blocks"]
    if not isinstance(blocks_raw, (list, tuple)) or not 1 <= len(blocks_raw) <= MAX_PLAN_BLOCKS:
        raise AgentContractError("limit_exceeded")
    blocks: list[DataBlock | TextBlock | InventoryBlock] = []
    for index, raw_block in enumerate(blocks_raw):
        if not isinstance(raw_block, Mapping):
            raise AgentContractError("invalid_shape")
        kind = raw_block.get("type")
        transition = None
        if "transitionBefore" in raw_block:
            if index == 0:
                raise AgentContractError("invalid_shape")
            transition = _phrase(raw_block["transitionBefore"], TRANSITION_IDS)
        if kind == "text":
            item = _keys(raw_block, required={"type", "source"}, optional={"transitionBefore"})
            tool_use_id, call = _parse_source(item["source"], SEARCH_DOCUMENTS)
            blocks.append(TextBlock(tool_use_id, transition, call))
        elif kind == "data":
            item = _keys(raw_block, required={"type", "groups"}, optional={"transitionBefore", "order", "aggregates"})
            data = _parse_data_block(item)
            blocks.append(DataBlock(data.groups, data.order, data.aggregates, transition))
        elif kind == "inventory":
            item = _keys(raw_block, required={"type", "source"}, optional={"transitionBefore", "order", "statusIs"})
            tool_use_id, call = _parse_source(item["source"], LIST_MATTER_DOCUMENTS)
            blocks.append(_parse_inventory_block(item, tool_use_id, transition, call))
        else:
            raise AgentContractError("invalid_shape")
    return AnswerPlan(intro, tuple(blocks))


PLAN_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "tool_use", "tool_result", "max_tokens", "max_iterations_exceeded", "guardrail_intervened", "content_filtered", "malformed_model_output", "malformed_tool_use", "model_context_window_exceeded"})


def closed_stop_reason(value: object) -> str | None:
    return None if value is None else value if isinstance(value, str) and value in PLAN_STOP_REASONS else "other"


PLAN_PARSE_STAGES = frozenset({"not_attempted", "json_decode", "size", "schema", "accepted"})
PLAN_DIAGNOSTIC_KEYS = frozenset({"planChars", "planHasJsonStart", "planHasJsonEnd", "planParseStage"})


def plan_diagnostics(text: object, error: str | None, *, attempted: bool) -> dict[str, object]:
    """Describe the actual strict parser outcome; never extract/repair a JSON envelope."""
    value = text if isinstance(text, str) else ""
    stage = ("not_attempted" if not attempted else "json_decode" if error == "invalid_json"
             else "size" if error == "too_large" else "schema" if error else "accepted")
    return {"planChars": len(value), "planHasJsonStart": "{" in value or "[" in value,
            "planHasJsonEnd": "}" in value or "]" in value, "planParseStage": stage}


__all__ = [name for name in globals() if not name.startswith("_")]
