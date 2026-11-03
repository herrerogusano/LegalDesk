"""Tenant- and matter-authorized retrieval from Amazon Bedrock Knowledge Bases.

The browser supplies an identity that has already been verified by the API
adapter and a matter selector. Effective scope is always resolved from the
authorization store; neither a tenant nor a vector filter is accepted from the
caller. Results are checked again after retrieval so a misconfigured or stale
index cannot return a passage from another scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import time
from typing import Any, Mapping, Protocol

from .authorization import (
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
    require_authorized_context,
)
from .documents import DocumentMetadataRepository
from .domain.models import DocumentStatus, MalwareScanStatus
from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
)


MAX_QUERY_LENGTH = 2_000
DEFAULT_NUMBER_OF_RESULTS = 5
# Grounding currently accepts at most 100,000 source characters.  Enforce the
# provider boundary immediately after retrieval so an unexpectedly large or
# malformed response cannot be handed to the resolver/writer first.
MAX_RETRIEVAL_SOURCE_CHARACTERS = 100_000


class RetrievalSubstepError(ValueError):
    """A retrieval post-check failed; ``code`` is a closed ``TelemetryErrorCode`` value and never carries content.

    Still a ``ValueError`` with the same messages as before, so every caller keeps failing closed exactly as it did; only the
    telemetry can now tell the sub-steps apart.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_OBJECT_DENIED = frozenset({"403", "accessdenied", "forbidden"})
_OBJECT_MISSING = frozenset({"404", "nosuchkey", "notfound"})


def _object_error_code(exc: BaseException) -> str:
    """Closed class of an object HEAD failure, from the provider error code only (never the message)."""

    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    code = error.get("Code") if isinstance(error, Mapping) else None
    if isinstance(code, str):
        if code.lower() in _OBJECT_DENIED:
            return "retrieval_object_denied"
        if code.lower() in _OBJECT_MISSING:
            return "retrieval_object_missing"
    return "retrieval_object_failed"


class BedrockKnowledgeBaseClient(Protocol):
    def retrieve(
        self,
        *,
        knowledgeBaseId: str,
        retrievalQuery: Mapping[str, str],
        retrievalConfiguration: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


class ObjectHeadStorage(Protocol):
    """Minimal storage boundary used to revalidate canonical source objects."""

    def head_object(self, *, key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    document_id: str
    source_uri: str | None
    page_number: int | None
    section: str | None
    source_metadata: Mapping[str, str]
    document_name: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievedPassage:
    text: str
    score: float | None
    citation: Citation


def build_matter_filter(context: RequestContext) -> dict[str, Any]:
    """Return the mandatory AND filter from a server-built request context."""

    context = require_authorized_context(context)
    return {
        "andAll": [
            {"equals": {"key": "tenantId", "value": context.tenant_id}},
            {"equals": {"key": "matterId", "value": context.matter_id}},
        ]
    }


def build_selected_document_filter(
    context: RequestContext, selected_document_id: str
) -> dict[str, Any]:
    """Constrain retrieval to one server-authorized document as well as matter."""

    context = require_authorized_context(context)
    if not isinstance(selected_document_id, str) or not selected_document_id.strip():
        raise ValueError("selected_document_id is invalid")
    selected_document_id = selected_document_id.strip()
    matter_filter = build_matter_filter(context)
    return {
        "andAll": [
            *matter_filter["andAll"],
            {"equals": {"key": "documentId", "value": selected_document_id}},
        ]
    }


def _metadata_string(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _optional_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _normalize_results(
    response: Mapping[str, Any],
    context: RequestContext,
    *,
    selected_document_id: str | None = None,
) -> tuple[RetrievedPassage, ...]:
    context = require_authorized_context(context)
    raw_results = response.get("retrievalResults")
    if not isinstance(raw_results, (list, tuple)):
        raise ValueError("retrievalResults is malformed")
    if len(raw_results) > DEFAULT_NUMBER_OF_RESULTS:
        raise ValueError("retrievalResults exceeds the configured result limit")

    normalized: list[RetrievedPassage] = []
    total_source_characters = 0
    for result in raw_results:
        if not isinstance(result, Mapping):
            raise ValueError("retrieval result is malformed")
        content = result.get("content")
        metadata = result.get("metadata")
        if not isinstance(content, Mapping) or not isinstance(metadata, Mapping):
            raise ValueError("retrieval result content or metadata is malformed")

        # Fail closed if scope-bearing metadata is absent or inconsistent.
        if (
            metadata.get("tenantId") != context.tenant_id
            or metadata.get("matterId") != context.matter_id
        ):
            continue
        document_id = _metadata_string(metadata, "documentId")
        document_name = (
            _metadata_string(metadata, "documentName")
            or _metadata_string(metadata, "document_name")
        )
        text = content.get("text")
        if document_id is None or not isinstance(text, str) or not text.strip():
            raise ValueError("retrieval result lacks citation fields")
        if selected_document_id is not None and document_id != selected_document_id:
            # The provider response is untrusted even when a server-side
            # filter was supplied.  Do not expose a passage from another
            # document if the index returns stale or forged metadata.
            raise ValueError("retrieval result is outside selected document")
        total_source_characters += len(text)
        if total_source_characters > MAX_RETRIEVAL_SOURCE_CHARACTERS:
            raise ValueError("retrieval sources exceed the grounding limit")

        location = result.get("location")
        s3_location = location.get("s3Location") if isinstance(location, Mapping) else None
        source_uri = None
        if isinstance(s3_location, Mapping):
            candidate_uri = s3_location.get("uri")
            if isinstance(candidate_uri, str) and candidate_uri.startswith("s3://"):
                source_uri = candidate_uri

        page_number = _optional_positive_int(
            metadata.get("x-amz-bedrock-kb-document-page-number")
        )
        section = (
            _metadata_string(metadata, "section")
            or _metadata_string(metadata, "sectionTitle")
            or _metadata_string(metadata, "x-amz-bedrock-kb-document-section")
        )
        source_metadata = {
            key: value
            for key in (
                "tenantId",
                "matterId",
                "documentId",
                "mediaType",
                "jurisdiction",
                "confidentiality",
            )
            if (value := _metadata_string(metadata, key)) is not None
        }
        if page_number is not None:
            source_metadata["pageNumber"] = str(page_number)
        if section is not None:
            source_metadata["section"] = section

        raw_score = result.get("score")
        if raw_score is None:
            score = None
        elif isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
            raise ValueError("retrieval score is malformed")
        else:
            try:
                score = float(raw_score)
            except (OverflowError, ValueError) as exc:
                raise ValueError("retrieval score is malformed") from exc
            if not isfinite(score):
                raise ValueError("retrieval score is outside the finite range")
        citation_id = f"citation-{len(normalized) + 1}"
        normalized.append(
            RetrievedPassage(
                text=text.strip(),
                score=score,
                citation=Citation(
                    citation_id=citation_id,
                    document_id=document_id,
                    document_name=document_name,
                    source_uri=source_uri,
                    page_number=page_number,
                    section=section,
                    source_metadata=source_metadata,
                ),
            )
        )
    return tuple(normalized)


def search_legal_documents(
    identity: VerifiedIdentity,
    requested_matter_id: str,
    query: str,
    *,
    authorization_store: AuthorizationStore,
    bedrock_client: BedrockKnowledgeBaseClient,
    knowledge_base_id: str,
    correlation_id: str | None = None,
    telemetry_sink: TelemetrySink | None = None,
    metadata_repository: DocumentMetadataRepository | None = None,
    object_storage: ObjectHeadStorage | None = None,
) -> tuple[RetrievedPassage, ...]:
    """Authorize a matter, retrieve with a server-built filter, and normalize.

    An empty tuple represents a valid query with no authorized evidence. AWS
    clients are injected so unit tests do not need credentials or network calls.
    """

    context = build_request_context(
        identity,
        requested_matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    return _retrieve_with_context(
        context,
        query,
        bedrock_client=bedrock_client,
        knowledge_base_id=knowledge_base_id,
        telemetry_sink=telemetry_sink,
        metadata_repository=metadata_repository,
        object_storage=object_storage,
    )


def _retrieve_with_context(
    context: RequestContext,
    query: str,
    *,
    bedrock_client: BedrockKnowledgeBaseClient,
    knowledge_base_id: str,
    telemetry_sink: TelemetrySink | None = None,
    metadata_repository: DocumentMetadataRepository | None = None,
    object_storage: ObjectHeadStorage | None = None,
    selected_document_id: str | None = None,
) -> tuple[RetrievedPassage, ...]:
    """Retrieve only with the RequestContext just authorized by the server."""

    context = require_authorized_context(context)
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
        raise ValueError("query is empty or outside the allowed length")
    if not isinstance(knowledge_base_id, str) or not knowledge_base_id.strip():
        raise ValueError("knowledge_base_id is not configured")
    if object_storage is not None and metadata_repository is None:
        raise ValueError("object_storage requires metadata_repository")
    if selected_document_id is not None and (
        not isinstance(selected_document_id, str) or not selected_document_id.strip()
    ):
        raise ValueError("selected_document_id is invalid")
    selected_document_id = selected_document_id.strip() if selected_document_id is not None else None

    started_at = time.perf_counter()
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.RETRIEVAL,
        context.correlation_id,
        TelemetryOutcome.STARTED,
        operation="knowledge_base_retrieve",
    )
    try:
        response = bedrock_client.retrieve(
            knowledgeBaseId=knowledge_base_id,
            retrievalQuery={"text": query.strip()},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": DEFAULT_NUMBER_OF_RESULTS,
                    "filter": (
                        build_selected_document_filter(context, selected_document_id)
                        if selected_document_id is not None
                        else build_matter_filter(context)
                    ),
                }
            },
        )
    except Exception:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.RETRIEVAL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="knowledge_base_retrieve",
            error_code="retrieval_failed",
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="knowledge_base_retrieve",
            error_code="retrieval_failed",
        )
        raise
    if not isinstance(response, Mapping):
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.RETRIEVAL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="knowledge_base_retrieve",
            error_code="retrieval_invalid_response",
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="knowledge_base_retrieve",
            error_code="retrieval_invalid_response",
        )
        raise ValueError("retrieval response is malformed")
    error_code = "retrieval_invalid_response"
    try:
        try:
            results = _normalize_results(
                response, context, selected_document_id=selected_document_id
            )
        except Exception as exc:
            raise RetrievalSubstepError("retrieval_results_malformed", str(exc)) from exc
        if metadata_repository is not None:
            for passage in results:
                try:
                    document = metadata_repository.get_for_scope(
                        tenant_id=context.tenant_id,
                        matter_id=context.matter_id,
                        document_id=passage.citation.document_id,
                    )
                except Exception as exc:
                    raise RetrievalSubstepError(
                        "retrieval_document_lookup_failed", "retrieval result document lookup failed"
                    ) from exc
                # A stale vector can remain after object/metadata deletion until
                # the next KB sync. Never pass it to a resolver/writer: fail
                # closed for the whole response. The closed code says which of
                # the three conditions it was.
                if document is None:
                    raise RetrievalSubstepError("retrieval_document_missing", "retrieval result document is unavailable")
                if document.status is not DocumentStatus.INDEXED:
                    raise RetrievalSubstepError("retrieval_document_not_indexed", "retrieval result document is unavailable")
                if document.malware_scan_status is not MalwareScanStatus.CLEAN:
                    raise RetrievalSubstepError("retrieval_document_not_clean", "retrieval result document is unavailable")
                if object_storage is not None:
                    # The provider URI is untrusted retrieval data.  Resolve
                    # the key exclusively from the authorized, live Document
                    # record so a stale vector cannot redirect this HEAD.
                    try:
                        head = object_storage.head_object(key=document.s3_key)
                        if not isinstance(head, Mapping):
                            raise ValueError("object HEAD response is malformed")
                    except Exception as exc:
                        raise RetrievalSubstepError(
                            _object_error_code(exc), "retrieval result document object is unavailable"
                        ) from exc
    except Exception as exc:
        if isinstance(exc, RetrievalSubstepError):
            error_code = exc.code
        # Provider-shaped content is untrusted.  A malformed score/page or
        # another normalization failure must close the started retrieval span
        # before failing closed, rather than leaving an open started event.
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.RETRIEVAL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="knowledge_base_retrieve",
            error_code=error_code,
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="knowledge_base_retrieve",
            error_code=error_code,
        )
        raise
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.RETRIEVAL,
        context.correlation_id,
        TelemetryOutcome.SUCCEEDED if results else TelemetryOutcome.NOT_FOUND,
        started_at=started_at,
        operation="knowledge_base_retrieve",
        count=len(results),
    )
    return results
