"""Deterministic validation and rendering of the agent's answer plan (ADR-021 D3, D5).

Everything here is a pure function of ``(loop outcome, final agent output, tool
results of the same loop)``: no clock, no network, no model. The worker (T4)
collects the evidence from the Harness stream, calls :func:`resolve_output`, and
runs the direct RAG path when the result says so.

Guarantees:

* every sentence of a data block comes from ``agent_templates``; the agent only
  chose ids, references, criteria, order and aggregate types;
* every value is the value a tool returned in this same loop, with its citation;
* min, max and count are computed here, never by the agent;
* text blocks are the verbatim ``search_documents`` answer the plan points at;
* anything outside the contract drops the block (or the plan) and is reported as
  a closed reason code, never repaired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Mapping

from . import agent_templates as tpl
from .agent_contracts import (
    AgentContractError,
    AggregateRequest,
    AnswerPlan,
    Criterion,
    DataBlock,
    DocumentFields,
    ExtractedField,
    ExtractedFieldsResult,
    FieldStatus,
    Group,
    InventoryBlock,
    InventoryResult,
    Order,
    SearchDocumentsResult,
    TextBlock,
    parse_answer_plan,
)

MAX_RENDERED_VALUE_CHARS = 500
# One output can carry at most this many distinct citations. It is also the
# number of citation handles the worker may create (``agent_citations``), so a
# plan that validates here can always be turned into handles.
MAX_OUTPUT_CITATIONS = 32
# A data line cites the first evidence anchor of its field; a second anchor
# would double the citations without adding a different fact.
MAX_CITATIONS_PER_FIELD = 1


class OutputPath(StrEnum):
    GROUNDED = "grounded"          # search_documents text, verbatim
    DATA = "data"                  # backend-written data block(s)
    INVENTORY = "inventory"        # backend-written listing of the matter documents
    MIXED = "mixed"                # data and text blocks, each with its own citations
    TOOL_RESULT = "tool_result"    # plan unusable; a valid search_documents result is reused
    RAG_FALLBACK = "rag_fallback"  # nothing usable; the caller runs the direct RAG path


class LoopOutcome(StrEnum):
    COMPLETED = "completed"
    NO_TOOL_CALLED = "no_tool_called"
    FAILED = "failed"
    ITERATIONS_EXHAUSTED = "iterations_exhausted"
    TIMEOUT = "timeout"


class FallbackReason(StrEnum):
    NO_TOOL_CALLED = "no_tool_called"
    LOOP_FAILED = "loop_failed"
    ITERATIONS_EXHAUSTED = "iterations_exhausted"
    TIMEOUT = "timeout"
    PLAN_MISSING = "plan_missing"
    PLAN_INVALID = "plan_invalid"
    NO_VALID_BLOCKS = "no_valid_blocks"


class BlockError(StrEnum):
    """Why a block was omitted (closed vocabulary, safe as metadata)."""

    UNKNOWN_TOOL_USE_ID = "unknown_tool_use_id"
    DUPLICATE_TEXT_BLOCK = "duplicate_text_block"
    DUPLICATE_INVENTORY_BLOCK = "duplicate_inventory_block"
    MISSING_CITATION = "missing_citation"
    UNKNOWN_REF = "unknown_ref"
    EMPTY_REF = "empty_ref"
    CRITERION_NOT_MET = "criterion_not_met"
    ORDER_INVALID = "order_invalid"
    AGGREGATE_INVALID = "aggregate_invalid"
    TOO_MANY_CITATIONS = "too_many_citations"


_LOOP_REASON = {
    LoopOutcome.NO_TOOL_CALLED: FallbackReason.NO_TOOL_CALLED,
    LoopOutcome.FAILED: FallbackReason.LOOP_FAILED,
    LoopOutcome.ITERATIONS_EXHAUSTED: FallbackReason.ITERATIONS_EXHAUSTED,
    LoopOutcome.TIMEOUT: FallbackReason.TIMEOUT,
}


class _BlockInvalid(Exception):
    def __init__(self, error: BlockError) -> None:
        self.error = error
        super().__init__(error.value)


# ------------------------------------------------------------------ evidence

@dataclass(frozen=True, slots=True)
class LoopEvidence:
    """Valid tool results of one agent loop (the only data a plan may use)."""

    search_results: Mapping[str, SearchDocumentsResult] = field(default_factory=dict)
    documents: Mapping[str, DocumentFields] = field(default_factory=dict)
    inventories: Mapping[str, InventoryResult] = field(default_factory=dict)

    @classmethod
    def from_results(
        cls,
        search_results: Iterable[tuple[str, SearchDocumentsResult]] = (),
        extracted: Iterable[ExtractedFieldsResult] = (),
        inventories: Iterable[tuple[str, InventoryResult]] = (),
    ) -> "LoopEvidence":
        """Collect results in call order; later field values replace earlier ones."""

        merged: dict[str, DocumentFields] = {}
        for result in extracted:
            for document_id, document in result.documents.items():
                previous = merged.get(document_id)
                fields = {**previous.fields, **document.fields} if previous else dict(document.fields)
                merged[document_id] = DocumentFields(document_id, document.document_name, document.document_type, fields)
        return cls(dict(search_results), merged, dict(inventories))


# ------------------------------------------------------------------- output

@dataclass(frozen=True, slots=True)
class RenderedCitation:
    citation_id: str
    document_id: str
    document_name: str | None
    page_number: int | None
    section: str | None
    passage: str
    is_idp: bool


@dataclass(frozen=True, slots=True)
class DataLine:
    text: str
    pending: bool
    citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DataGroup:
    title: str
    lines: tuple[DataLine, ...]


@dataclass(frozen=True, slots=True)
class AggregateLine:
    text: str
    citation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OutputBlock:
    kind: str  # intro | transition | text | data | notice
    text: str | None = None
    evidence_status: str | None = None
    groups: tuple[DataGroup, ...] = ()
    aggregates: tuple[AggregateLine, ...] = ()
    citation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OmittedBlock:
    index: int
    error: BlockError


@dataclass(frozen=True, slots=True)
class AnswerOutput:
    path: OutputPath
    blocks: tuple[OutputBlock, ...] = ()
    citations: tuple[RenderedCitation, ...] = ()
    suggest_review: str | None = None
    fallback_reason: FallbackReason | None = None
    plan_error: str | None = None
    omitted: tuple[OmittedBlock, ...] = ()

    def public_dict(self) -> dict[str, object]:
        """Browser-facing shape: citation ids only, never passages or quotes."""

        return {
            "path": self.path.value,
            "blocks": [
                {key: value for key, value in {
                    "type": block.kind,
                    "text": block.text,
                    "evidenceStatus": block.evidence_status,
                    "groups": [
                        {"title": group.title, "lines": [
                            {"text": line.text, "pending": line.pending, "citationIds": list(line.citation_ids)}
                            for line in group.lines
                        ]}
                        for group in block.groups
                    ] if block.groups else None,
                    "aggregates": [
                        {"text": line.text, "citationIds": list(line.citation_ids)} for line in block.aggregates
                    ] if block.aggregates else None,
                    "citationIds": list(block.citation_ids) if block.kind in {"text", "data"} else None,
                }.items() if value is not None}
                for block in self.blocks
            ],
            "citations": [
                {"citationId": c.citation_id, "documentId": c.document_id, "documentName": c.document_name,
                 "pageNumber": c.page_number, "section": c.section}
                for c in self.citations
            ],
            "suggestReview": {"reasonCode": self.suggest_review} if self.suggest_review else None,
        }

    def plain_text(self) -> str:
        """The answer as the user reads it, for conversation memory (final answer only)."""

        parts: list[str] = []
        for block in self.blocks:
            if block.text:
                parts.append(block.text)
            for group in block.groups:
                parts.append(group.title)
                parts.extend(line.text for line in group.lines)
            parts.extend(line.text for line in block.aggregates)
        return "\n".join(parts)

    def metadata(self) -> dict[str, object]:
        """Content-free facts for telemetry (ADR-021 D9)."""

        return {
            "outputPath": self.path.value,
            "fallbackReason": self.fallback_reason.value if self.fallback_reason else None,
            "planError": self.plan_error,
            "omittedBlocks": [{"index": item.index, "error": item.error.value} for item in self.omitted],
            "blockCount": len(self.blocks),
            "citationCount": len(self.citations),
        }


# ------------------------------------------------------------ value helpers

def _as_date(value: object) -> date | None:
    if isinstance(value, str) and len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = Decimal(str(value))
    return number if number.is_finite() else None


def _comparable(value: object) -> tuple[str, date | Decimal] | None:
    """A value that can be ordered, tagged by kind so kinds are never mixed."""

    parsed_date = _as_date(value)
    if parsed_date is not None:
        return "date", parsed_date
    parsed_number = _as_number(value)
    if parsed_number is not None:
        return "number", parsed_number
    return None


def _format_number(number: Decimal) -> str:
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_value(value: object) -> str:
    if isinstance(value, bool):
        return tpl.VALUES["true" if value else "false"]
    if isinstance(value, list):
        text = ", ".join(str(item) for item in value)
    elif isinstance(value, float):
        text = _format_number(Decimal(str(value)))
    else:
        text = str(value)
    return text if len(text) <= MAX_RENDERED_VALUE_CHARS else text[: MAX_RENDERED_VALUE_CHARS - 1] + "…"


def _label(field_name: str) -> str:
    return tpl.FIELD_LABELS[field_name]


# ----------------------------------------------------------------- criteria

def evaluate_criterion(criterion: Criterion, document: DocumentFields) -> str | None:
    """``"fact"``, ``"pending"`` (decided on a provisional value) or ``None``."""

    params = criterion.params
    if criterion.id == "all_documents":
        return "fact"
    if criterion.id == "document_type_is":
        return "fact" if document.document_type == params["documentType"] else None
    field_name = {
        "expires_in_period": "explicit_expiration_date",
        "starts_in_period": "effective_date",
        "automatic_renewal": "automatic_renewal",
        "no_automatic_renewal": "automatic_renewal",
        "amount_in_range": "amount",
    }[criterion.id]
    extracted = document.fields.get(field_name)
    if extracted is None or extracted.status is FieldStatus.EMPTY:
        return None
    value = extracted.value
    if criterion.id in {"expires_in_period", "starts_in_period"}:
        parsed = _as_date(value)
        met = parsed is not None and ("from" not in params or parsed >= params["from"]) and (
            "to" not in params or parsed <= params["to"])
    elif criterion.id == "amount_in_range":
        number = _as_number(value)
        met = number is not None and ("min" not in params or number >= params["min"]) and (
            "max" not in params or number <= params["max"])
    elif criterion.id == "automatic_renewal":
        met = value is True
    else:
        met = value is False
    if not met:
        return None
    return "fact" if extracted.status is FieldStatus.FACT else "pending"


def group_title(criterion: Criterion) -> str:
    params = {name: (value.isoformat() if isinstance(value, date) else
                     _format_number(value) if isinstance(value, Decimal) else value)
              for name, value in criterion.params.items()}
    return tpl.fill(tpl.GROUP_TITLES[criterion.title_key], **params)


# ----------------------------------------------------------- block rendering

def _idp_citations(document: DocumentFields, extracted: ExtractedField) -> tuple[RenderedCitation, ...]:
    # Same id scheme the direct selected-field answer uses (``http_app``).
    return tuple(
        RenderedCitation(
            citation_id=f"idp-{document.document_id}-{extracted.name}-{index}",
            document_id=document.document_id, document_name=document.document_name,
            page_number=anchor.page, section=None, passage=anchor.quote, is_idp=True,
        )
        for index, anchor in enumerate(extracted.evidence[:MAX_CITATIONS_PER_FIELD], start=1)
    )


@dataclass(frozen=True, slots=True)
class _Line:
    document: DocumentFields
    extracted: ExtractedField
    pending: bool
    citations: tuple[RenderedCitation, ...]


def _resolve_line(documents: Mapping[str, DocumentFields], document_id: str, field_name: str) -> tuple[DocumentFields, ExtractedField]:
    document = documents.get(document_id)
    extracted = document.fields.get(field_name) if document else None
    if document is None or extracted is None:
        raise _BlockInvalid(BlockError.UNKNOWN_REF)
    if extracted.status is FieldStatus.EMPTY:
        raise _BlockInvalid(BlockError.EMPTY_REF)
    if not extracted.evidence:
        raise _BlockInvalid(BlockError.MISSING_CITATION)
    return document, extracted


def _sort_key_by_name(line: _Line) -> tuple[str, str, str]:
    return line.document.document_name.casefold(), line.document.document_id, line.extracted.name


def _order_lines(lines: list[_Line], order: Order | None) -> list[_Line]:
    if order is None:
        return lines
    reverse = order.direction == "desc"
    if order.by == "document_name":
        return sorted(lines, key=_sort_key_by_name, reverse=reverse)
    keyed: list[tuple[tuple[str, date | Decimal] | None, _Line]] = []
    for line in lines:
        source = line.document.fields.get(order.field or "")
        comparable = None
        if source is not None and source.status is not FieldStatus.EMPTY:
            comparable = _comparable(source.value)
            if comparable is None:
                raise _BlockInvalid(BlockError.ORDER_INVALID)
        keyed.append((comparable, line))
    kinds = {key[0] for key, _ in keyed if key is not None}
    if len(kinds) > 1:
        raise _BlockInvalid(BlockError.ORDER_INVALID)
    # Name order first, then a stable sort by value: ties keep the name order
    # in both directions, and documents without the field always come last.
    with_value = sorted(((key[1], line) for key, line in keyed if key is not None), key=lambda item: _sort_key_by_name(item[1]))
    with_value.sort(key=lambda item: item[0], reverse=reverse)
    ordered = [line for _, line in with_value]
    ordered.extend(sorted((line for key, line in keyed if key is None), key=_sort_key_by_name))
    return ordered


def _aggregate_line(request: AggregateRequest, lines: list[_Line]) -> AggregateLine:
    if request.type == "count":
        scoped = [line for line in lines if request.field is None or line.extracted.name == request.field]
        if not scoped:
            raise _BlockInvalid(BlockError.AGGREGATE_INVALID)
        count = len({line.document.document_id for line in scoped})
        return AggregateLine(tpl.fill(tpl.AGGREGATES["count"], value=count), ())
    scoped = [line for line in lines if line.extracted.name == request.field]
    if not scoped:
        raise _BlockInvalid(BlockError.AGGREGATE_INVALID)  # aggregate over a field the block does not show
    facts = [line for line in scoped if line.extracted.status is FieldStatus.FACT]
    pending = len(scoped) - len(facts)
    comparables = [(_comparable(line.extracted.value), line) for line in facts]
    if any(key is None for key, _ in comparables) or len({key[0] for key, _ in comparables if key}) > 1:
        raise _BlockInvalid(BlockError.AGGREGATE_INVALID)
    field_label = _label(request.field or "")
    if comparables:
        choose = min if request.type == "min" else max
        best = choose(comparables, key=lambda item: (item[0][1], _sort_key_by_name(item[1])))  # type: ignore[index]
        value = best[0][1]  # type: ignore[index]
        rendered = value.isoformat() if isinstance(value, date) else _format_number(value)
        text = tpl.fill(tpl.AGGREGATES[request.type], field=field_label, value=rendered)
        ids = tuple(c.citation_id for c in best[1].citations)
    else:
        text = tpl.fill(tpl.AGGREGATES[f"{request.type}_none"], field=field_label)
        ids = ()
    if pending:
        text = tpl.fill(tpl.AGGREGATES["pending_excluded"], text=text, pending=pending)
    return AggregateLine(text, ids)


def render_data_block(block: DataBlock, evidence: LoopEvidence) -> tuple[OutputBlock, list[RenderedCitation]]:
    """Validate and render one data block; raises ``_BlockInvalid`` on any violation."""

    documents = evidence.documents
    all_lines: list[_Line] = []
    groups: list[DataGroup] = []
    citations: dict[str, RenderedCitation] = {}
    for group in block.groups:
        lines: list[_Line] = []
        for ref in group.refs:
            document, extracted = _resolve_line(documents, ref.document_id, ref.field)
            decided_on = evaluate_criterion(group.criterion, document)
            if decided_on is None:
                raise _BlockInvalid(BlockError.CRITERION_NOT_MET)
            line_citations = _idp_citations(document, extracted)
            lines.append(_Line(document, extracted, extracted.status is FieldStatus.PENDING_REVIEW or decided_on == "pending",
                               line_citations))
        lines = _order_lines(lines, block.order)
        all_lines.extend(lines)
        groups.append(DataGroup(group_title(group.criterion), tuple(
            DataLine(
                tpl.fill(tpl.LINES["pending" if line.pending else "fact"],
                         document=line.document.document_name, field=_label(line.extracted.name),
                         value=render_value(line.extracted.value)),
                line.pending, tuple(c.citation_id for c in line.citations),
            )
            for line in lines
        )))
        for line in lines:
            citations.update({c.citation_id: c for c in line.citations})
    aggregates = tuple(_aggregate_line(request, all_lines) for request in block.aggregates)
    if not citations:
        raise _BlockInvalid(BlockError.MISSING_CITATION)
    if len(citations) > MAX_OUTPUT_CITATIONS:
        raise _BlockInvalid(BlockError.TOO_MANY_CITATIONS)
    return (
        OutputBlock("data", groups=tuple(groups), aggregates=aggregates, citation_ids=tuple(citations)),
        list(citations.values()),
    )


def render_text_block(
    block: TextBlock, evidence: LoopEvidence, used: set[str]
) -> tuple[OutputBlock, list[RenderedCitation]]:
    result = evidence.search_results.get(block.tool_use_id)
    if result is None:
        raise _BlockInvalid(BlockError.UNKNOWN_TOOL_USE_ID)
    if block.tool_use_id in used:
        raise _BlockInvalid(BlockError.DUPLICATE_TEXT_BLOCK)
    return _text_output(result)


def _text_output(result: SearchDocumentsResult) -> tuple[OutputBlock, list[RenderedCitation]]:
    if result.evidence_status != "insufficient_evidence" and not result.citations:
        raise _BlockInvalid(BlockError.MISSING_CITATION)
    citations = [
        RenderedCitation(c.citation_id, c.document_id, c.document_name, c.page_number, c.section, c.passage, False)
        for c in result.citations
    ]
    # ``result.answer`` is passed through untouched: the agent never rewrites it.
    return (
        OutputBlock("text", text=result.answer, evidence_status=result.evidence_status,
                    citation_ids=tuple(c.citation_id for c in citations)),
        citations,
    )


def render_inventory_block(
    block: InventoryBlock, evidence: LoopEvidence, used: set[str]
) -> tuple[OutputBlock, list[RenderedCitation]]:
    """List the documents a ``list_matter_documents`` call returned, from templates only.

    The agent chose the call, an optional status filter and an order; name, file
    type and status come from the tool result. The listing is metadata, not
    document content, so it carries no passage citations.
    """

    result = evidence.inventories.get(block.tool_use_id)
    if result is None:
        raise _BlockInvalid(BlockError.UNKNOWN_TOOL_USE_ID)
    if block.tool_use_id in used:
        raise _BlockInvalid(BlockError.DUPLICATE_INVENTORY_BLOCK)
    documents = [item for item in result.documents if not block.statuses or item.status in block.statuses]
    if block.order_by == "uploaded_at":
        documents.sort(key=lambda item: (item.uploaded_at, item.name.casefold(), item.document_id),
                       reverse=block.direction == "desc")
    else:
        documents.sort(key=lambda item: (item.name.casefold(), item.document_id), reverse=block.direction == "desc")
    if not documents:
        return OutputBlock("inventory", text=tpl.NOTICES["inventory_empty"]), []
    lines = tuple(
        DataLine(
            tpl.fill(tpl.INVENTORY_LINES["document"], document=item.name,
                     media=tpl.INVENTORY_MEDIA.get(item.media_type, tpl.INVENTORY_MEDIA["other"]),
                     status=tpl.INVENTORY_STATUS.get(item.status, tpl.INVENTORY_STATUS["other"])),
            False, (),
        )
        for item in documents
    )
    count = AggregateLine(tpl.fill(tpl.AGGREGATES["count"], value=len(documents)), ())
    return OutputBlock("inventory", groups=(DataGroup(tpl.INVENTORY_TITLE, lines),), aggregates=(count,)), []


# ------------------------------------------------------------- output tree

def _salvage(evidence: LoopEvidence, reason: FallbackReason, plan_error: str | None = None,
             omitted: tuple[OmittedBlock, ...] = ()) -> AnswerOutput:
    """D5: reuse a valid ``search_documents`` result before falling back to RAG."""

    for result in reversed(list(evidence.search_results.values())):
        try:
            block, citations = _text_output(result)
        except _BlockInvalid:
            continue
        return AnswerOutput(
            OutputPath.TOOL_RESULT, blocks=(block,), citations=tuple(citations), fallback_reason=reason,
            plan_error=plan_error, omitted=omitted,
        )
    return AnswerOutput(OutputPath.RAG_FALLBACK, fallback_reason=reason, plan_error=plan_error, omitted=omitted)


def render_plan(plan: AnswerPlan, evidence: LoopEvidence) -> AnswerOutput | None:
    """Validate and render a parsed plan; ``None`` when no block survives."""

    rendered: list[tuple[str | None, OutputBlock]] = []
    citations: dict[str, RenderedCitation] = {}
    omitted: list[OmittedBlock] = []
    used_text: set[str] = set()
    used_inventory: set[str] = set()
    for index, block in enumerate(plan.blocks):
        try:
            if isinstance(block, TextBlock):
                output, block_citations = render_text_block(block, evidence, used_text)
            elif isinstance(block, InventoryBlock):
                output, block_citations = render_inventory_block(block, evidence, used_inventory)
            else:
                output, block_citations = render_data_block(block, evidence)
            # The whole output shares one citation budget; a block that would
            # exceed it is omitted like any other invalid block, so a plan
            # that validates can always be turned into citation handles.
            if len(citations.keys() | {c.citation_id for c in block_citations}) > MAX_OUTPUT_CITATIONS:
                raise _BlockInvalid(BlockError.TOO_MANY_CITATIONS)
        except _BlockInvalid as invalid:
            omitted.append(OmittedBlock(index, invalid.error))
            continue
        if isinstance(block, TextBlock):
            used_text.add(block.tool_use_id)
        elif isinstance(block, InventoryBlock):
            used_inventory.add(block.tool_use_id)
        rendered.append((block.transition_before, output))
        citations.update({c.citation_id: c for c in block_citations})
    if not rendered:
        return None
    kinds = {output.kind for _, output in rendered}
    path = (
        OutputPath.GROUNDED if kinds == {"text"}
        else OutputPath.DATA if kinds == {"data"}
        else OutputPath.INVENTORY if kinds == {"inventory"}
        else OutputPath.MIXED
    )
    blocks: list[OutputBlock] = []
    # A verbatim grounded answer is returned as is: no backend framing around it.
    if plan.intro and path is not OutputPath.GROUNDED:
        blocks.append(OutputBlock("intro", text=tpl.INTRODUCTIONS[plan.intro]))
    for position, (transition, output) in enumerate(rendered):
        if transition and position > 0:
            blocks.append(OutputBlock("transition", text=tpl.TRANSITIONS[transition]))
        blocks.append(output)
    if omitted:
        notice = tpl.NOTICES["omitted_block"] if len(omitted) == 1 else tpl.fill(
            tpl.NOTICES["omitted_blocks"], count=len(omitted))
        blocks.append(OutputBlock("notice", text=notice))
    return AnswerOutput(
        path, blocks=tuple(blocks), citations=tuple(citations.values()), suggest_review=plan.suggest_review,
        omitted=tuple(omitted),
    )


def resolve_output(
    *,
    loop: LoopOutcome,
    plan: str | bytes | Mapping[str, object] | None,
    evidence: LoopEvidence,
) -> AnswerOutput:
    """The ADR-021 D5 output tree.

    1. plan valid and at least one block valid -> grounded / data / mixed;
    2. otherwise a valid ``search_documents`` result of this loop, as is;
    3. otherwise the caller runs the direct RAG path (``RAG_FALLBACK``).

    The reason for 2 and 3 is always recorded; nothing is ever repaired.
    """

    if loop is not LoopOutcome.COMPLETED:
        return _salvage(evidence, _LOOP_REASON[loop])
    if plan is None or (isinstance(plan, (str, bytes)) and not plan.strip()):
        return _salvage(evidence, FallbackReason.PLAN_MISSING)
    try:
        parsed = parse_answer_plan(plan)
    except AgentContractError as error:
        return _salvage(evidence, FallbackReason.PLAN_INVALID, plan_error=error.code)
    output = render_plan(parsed, evidence)
    if output is None:
        omitted = tuple(
            OmittedBlock(index, error) for index, error in _all_errors(parsed, evidence)
        )
        return _salvage(evidence, FallbackReason.NO_VALID_BLOCKS, omitted=omitted)
    return output


def _all_errors(plan: AnswerPlan, evidence: LoopEvidence) -> list[tuple[int, BlockError]]:
    errors: list[tuple[int, BlockError]] = []
    used: set[str] = set()
    for index, block in enumerate(plan.blocks):
        try:
            if isinstance(block, TextBlock):
                render_text_block(block, evidence, used)
            elif isinstance(block, InventoryBlock):
                render_inventory_block(block, evidence, set())
            else:
                render_data_block(block, evidence)
        except _BlockInvalid as invalid:
            errors.append((index, invalid.error))
    return errors


__all__ = [name for name in globals() if not name.startswith("_")]
