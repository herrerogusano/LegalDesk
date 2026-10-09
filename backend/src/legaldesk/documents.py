"""Authorized document upload and metadata boundaries.

The service in this module is deliberately HTTP-neutral.  An API adapter can
translate a multipart request into :class:`UploadRequest`, but it cannot
choose the effective tenant, matter, document ID, or object key.  Bodies go to
object storage only; the metadata repository receives identifiers and safe
metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import PurePath
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote
from uuid import UUID, uuid4

from .authorization import (
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
    require_authorized_context,
)
from .domain.models import Document, DocumentStatus, MalwareScanStatus, utc_now


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
ALLOWED_MEDIA_TYPES = frozenset({"application/pdf", "text/plain"})
ALLOWED_CONFIDENTIALITY = frozenset({"public-fictional", "fictional-internal"})
MAX_FILENAME_LENGTH = 255
MAX_METADATA_TEXT_LENGTH = 256
MAX_BEDROCK_CUSTOM_METADATA_BYTES = 1024
MAX_BEDROCK_CUSTOM_METADATA_KEYS = 35
MIN_PRESIGNED_URL_EXPIRES_SECONDS = 60
MAX_PRESIGNED_URL_EXPIRES_SECONDS = 900
MEDIA_TYPE_EXTENSIONS = {"application/pdf": ".pdf", "text/plain": ".txt"}


class DocumentError(Exception):
    """Base class for expected document pipeline errors."""


class DocumentValidationError(DocumentError, ValueError):
    """The upload does not satisfy the local safety policy."""


class DocumentStorageError(DocumentError):
    """Object storage failed; callers must not treat the upload as complete."""


class DocumentMetadataError(DocumentError):
    """Metadata persistence failed during the upload lifecycle."""


class DocumentConcurrencyError(DocumentError):
    """A conditional document transition lost a race; callers fail closed."""


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """Untrusted upload fields normalized at the service boundary."""

    filename: str
    media_type: str
    body: bytes | None = None
    file_size_bytes: int | None = None
    jurisdiction: str = "fictional"
    document_date: str = "2099-01-01"
    confidentiality: str = "fictional-internal"


class ObjectStorage(Protocol):
    def put_object(
        self,
        *,
        key: str,
        body: bytes,
        media_type: str,
        metadata: Mapping[str, str],
    ) -> None: ...

    def delete_object(self, *, key: str) -> None: ...

    def copy_object(self, *, source_key: str, destination_key: str) -> None: ...

    def generate_presigned_put_url(
        self,
        *,
        key: str,
        content_length: int,
        media_type: str,
        metadata: Mapping[str, str],
        expires_in: int,
    ) -> str: ...

    def head_object(self, *, key: str) -> Mapping[str, Any]: ...

    def read_object_bytes(self, *, key: str, max_bytes: int) -> bytes: ...

    def get_object_tagging(self, *, key: str) -> Mapping[str, str]: ...


class DocumentMetadataRepository(Protocol):
    def save(self, document: Document) -> None: ...

    def update_status(
        self, *, tenant_id: str, matter_id: str, document_id: str, status: DocumentStatus
    ) -> Document: ...

    def apply_malware_scan_result(
        self,
        *,
        tenant_id: str,
        matter_id: str,
        document_id: str,
        result_status: MalwareScanStatus,
        etag: str | None,
        version_id: str | None,
    ) -> Document: ...

    def get_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> Document | None: ...

    def list_for_scope(
        self, *, tenant_id: str, matter_id: str, limit: int | None = None
    ) -> Sequence[Document]: ...

    def delete_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> None: ...


def document_partition_key(tenant_id: str, matter_id: str) -> str:
    return f"TENANT#{tenant_id}#MATTER#{matter_id}"


def document_sort_key(document_id: str) -> str:
    return f"DOCUMENT#{document_id}"


def build_document_key(
    context: RequestContext, document_id: str, media_type: str
) -> str:
    """Build an object key solely from server-derived scope and ID."""

    context = require_authorized_context(context)
    try:
        UUID(document_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("document_id must be a UUID") from exc
    try:
        extension = MEDIA_TYPE_EXTENSIONS[media_type]
    except (KeyError, TypeError) as exc:
        raise ValueError("media_type is not supported") from exc
    return (
        f"tenants/{context.tenant_id}/matters/{context.matter_id}/"
        f"documents/{document_id}/original{extension}"
    )


def build_quarantine_document_key(
    context: RequestContext, document_id: str, media_type: str
) -> str:
    """Build an upload-only key outside the Knowledge Base source prefix."""

    return f"quarantine/{build_document_key(context, document_id, media_type)}"


def _validate_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DocumentValidationError(f"{field_name} is required")
    if len(value) > MAX_METADATA_TEXT_LENGTH:
        raise DocumentValidationError(f"{field_name} is too long")


def validate_upload(request: UploadRequest) -> None:
    validate_upload_metadata(request)
    if not isinstance(request.body, bytes):
        raise DocumentValidationError("body must be bytes")
    if not request.body or len(request.body) > MAX_DOCUMENT_BYTES:
        raise DocumentValidationError("body size is outside the allowed limit")
    if request.media_type == "application/pdf" and not request.body.startswith(b"%PDF-"):
        raise DocumentValidationError("PDF body has an invalid signature")
    if request.media_type == "text/plain":
        try:
            request.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError("text body must be valid UTF-8") from exc


def validate_upload_metadata(request: UploadRequest) -> None:
    """Validate presignable metadata without requiring the document body."""

    if request.body is not None and not isinstance(request.body, bytes):
        raise DocumentValidationError("body must be bytes")
    if request.file_size_bytes is None or (
        not isinstance(request.file_size_bytes, int)
        or isinstance(request.file_size_bytes, bool)
        or request.file_size_bytes <= 0
        or request.file_size_bytes > MAX_DOCUMENT_BYTES
    ):
        raise DocumentValidationError("file_size_bytes is outside the allowed limit")
    if request.media_type not in ALLOWED_MEDIA_TYPES:
        raise DocumentValidationError("media_type is not allowed")
    if (
        not isinstance(request.filename, str)
        or not request.filename.strip()
        or len(request.filename) > MAX_FILENAME_LENGTH
        or request.filename in {".", ".."}
        or PurePath(request.filename).name != request.filename
        or "\\" in request.filename
    ):
        raise DocumentValidationError("filename is invalid")
    suffix = PurePath(request.filename).suffix.lower()
    expected_suffix = {"application/pdf": ".pdf", "text/plain": ".txt"}[request.media_type]
    if suffix != expected_suffix:
        raise DocumentValidationError("filename extension does not match media_type")
    _validate_text(request.jurisdiction, "jurisdiction")
    _validate_text(request.document_date, "document_date")
    try:
        parsed_date = date.fromisoformat(request.document_date)
    except ValueError as exc:
        raise DocumentValidationError("document_date must be ISO-8601 date") from exc
    if parsed_date.isoformat() != request.document_date:
        raise DocumentValidationError("document_date must be ISO-8601 date")
    _validate_text(request.confidentiality, "confidentiality")
    if request.confidentiality not in ALLOWED_CONFIDENTIALITY:
        raise DocumentValidationError("confidentiality is not allowed")


def _safe_metadata(document: Document) -> dict[str, str]:
    """Metadata sent to S3; never include document content."""

    return {
        "tenant-id": document.tenant_id,
        "matter-id": document.matter_id,
        "document-id": document.document_id,
    }


def build_bedrock_metadata_attributes(document: Document) -> dict[str, str]:
    """Build and budget the flat custom metadata map used by Bedrock KB."""

    import json

    attributes = {
        "tenantId": document.tenant_id,
        "matterId": document.matter_id,
        "documentId": document.document_id,
        "documentName": document.name,
        "mediaType": document.media_type,
        "jurisdiction": document.jurisdiction,
        "confidentiality": document.confidentiality,
    }
    if len(attributes) > MAX_BEDROCK_CUSTOM_METADATA_KEYS:
        raise DocumentValidationError("document metadata has too many Bedrock attributes")
    if any(not isinstance(value, str) for value in attributes.values()):
        raise DocumentValidationError("document metadata attributes must be strings")
    try:
        serialized = json.dumps(
            attributes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as exc:
        raise DocumentValidationError("document metadata cannot be encoded as UTF-8") from exc
    if len(serialized) >= MAX_BEDROCK_CUSTOM_METADATA_BYTES:
        raise DocumentValidationError("document metadata exceeds the Bedrock Knowledge Bases budget")
    return attributes


def build_bedrock_metadata_sidecar(document: Document) -> bytes:
    """Serialize filterable Bedrock metadata without embedding it as content."""

    import json

    attributes = build_bedrock_metadata_attributes(document)
    payload = {
        "metadataAttributes": {
            key: {
                "value": {"type": "STRING", "stringValue": value},
                "includeForEmbedding": False,
            }
            for key, value in attributes.items()
        }
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _upload_headers(document: Document) -> dict[str, str]:
    """Headers required by the signed PUT request."""

    return {
        "Content-Type": document.media_type,
        "x-amz-meta-tenant-id": document.tenant_id,
        "x-amz-meta-matter-id": document.matter_id,
        "x-amz-meta-document-id": document.document_id,
        "x-amz-server-side-encryption": "AES256",
    }


def validate_uploaded_object(
    object_storage: ObjectStorage, document: Document, *, key: str | None = None
) -> Mapping[str, Any]:
    """Validate the server-owned object before any lifecycle promotion."""

    try:
        head = object_storage.head_object(key=key or document.s3_key)
    except Exception as exc:
        raise DocumentStorageError("uploaded object was not found") from exc
    content_length = head.get("ContentLength")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length <= 0
        or content_length > MAX_DOCUMENT_BYTES
        or content_length != document.file_size_bytes
    ):
        raise DocumentStorageError("uploaded object size is invalid")
    if head.get("ContentType") != document.media_type:
        raise DocumentStorageError("uploaded object media type is invalid")
    stored_metadata = head.get("Metadata")
    if not isinstance(stored_metadata, Mapping) or any(
        stored_metadata.get(name) != value
        for name, value in _safe_metadata(document).items()
    ):
        raise DocumentStorageError("uploaded object metadata is invalid")
    return head


def validate_uploaded_object_content(
    object_storage: ObjectStorage, document: Document, *, key: str | None = None
) -> None:
    """Validate the small, type-specific content boundary before indexing.

    The size was already checked by :func:`validate_uploaded_object`; the
    bounded read makes the content check safe for both S3 and local fixtures.
    The exception messages intentionally contain no body bytes.
    """

    try:
        body = object_storage.read_object_bytes(
            key=key or document.s3_key, max_bytes=MAX_DOCUMENT_BYTES
        )
    except DocumentStorageError:
        raise
    except Exception as exc:
        raise DocumentStorageError("uploaded object content could not be read") from exc
    if not isinstance(body, bytes) or not body or len(body) > MAX_DOCUMENT_BYTES:
        raise DocumentStorageError("uploaded object content size is invalid")
    if document.media_type == "application/pdf":
        if not body.startswith(b"%PDF-"):
            raise DocumentValidationError("uploaded PDF content is invalid")
        return
    if document.media_type == "text/plain":
        try:
            body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError("uploaded text content is invalid") from exc
        return
    raise DocumentValidationError("uploaded object media type is unsupported")


def write_bedrock_metadata_sidecar(object_storage: ObjectStorage, document: Document) -> None:
    """Write the indexing sidecar only after a trusted clean scan result."""

    sidecar = build_bedrock_metadata_sidecar(document)
    try:
        object_storage.put_object(
            key=f"{document.s3_key}.metadata.json",
            body=sidecar,
            media_type="application/json",
            metadata={"document-id": document.document_id},
        )
    except Exception as exc:
        raise DocumentStorageError("document metadata sidecar could not be stored") from exc


@dataclass(frozen=True, slots=True)
class UploadAuthorization:
    """Server-owned document details and the URL for the client's PUT."""

    document: Document
    upload_url: str
    method: str = "PUT"
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def document_id(self) -> str:
        return self.document.document_id

    @property
    def s3_key(self) -> str:
        return self.document.quarantine_s3_key or self.document.s3_key

    @property
    def presigned_url(self) -> str:
        """Descriptive alias used by HTTP adapters."""

        return self.upload_url


@dataclass(slots=True)
class DocumentPipeline:
    authorization_store: AuthorizationStore
    object_storage: ObjectStorage
    metadata_repository: DocumentMetadataRepository
    id_factory: Callable[[], UUID] = uuid4
    clock: Callable[[], datetime] = utc_now
    require_malware_scan: bool = False
    quarantine_uploads: bool = False

    def initiate_upload(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        request: UploadRequest,
        *,
        expires_in: int = 900,
        correlation_id: str | None = None,
    ) -> "UploadAuthorization":
        """Authorize an upload and return a presigned PUT without storing a body."""

        context = build_request_context(
            identity,
            requested_matter_id,
            self.authorization_store,
            correlation_id=correlation_id,
        )
        validate_upload_metadata(request)
        if (
            not isinstance(expires_in, int)
            or isinstance(expires_in, bool)
            or not MIN_PRESIGNED_URL_EXPIRES_SECONDS
            <= expires_in
            <= MAX_PRESIGNED_URL_EXPIRES_SECONDS
        ):
            raise DocumentValidationError("presigned URL expiry is outside the allowed range")
        document_id = str(self.id_factory())
        key = build_document_key(context, document_id, request.media_type)
        upload_key = build_quarantine_document_key(context, document_id, request.media_type) if self.quarantine_uploads else key
        document = Document(
            document_id=document_id,
            matter_id=context.matter_id,
            tenant_id=context.tenant_id,
            name=request.filename,
            s3_key=key,
            media_type=request.media_type,
            jurisdiction=request.jurisdiction,
            document_date=request.document_date,
            confidentiality=request.confidentiality,
            status=DocumentStatus.PENDING_UPLOAD,
            file_size_bytes=request.file_size_bytes or 0,
            uploaded_at=self.clock(),
            quarantine_s3_key=upload_key if self.quarantine_uploads else None,
            malware_scan_status=MalwareScanStatus.PENDING,
        )
        build_bedrock_metadata_attributes(document)
        try:
            self.metadata_repository.save(document)
        except Exception as exc:
            raise DocumentMetadataError("document metadata persistence failed") from exc
        try:
            upload_url = self.object_storage.generate_presigned_put_url(
                key=upload_key,
                content_length=document.file_size_bytes,
                media_type=document.media_type,
                metadata=_safe_metadata(document),
                expires_in=expires_in,
            )
        except Exception as exc:
            self._best_effort_failed_metadata(document)
            raise DocumentStorageError("document upload authorization failed") from exc
        return UploadAuthorization(
            document=document,
            upload_url=upload_url,
            headers=_upload_headers(document),
        )

    # Explicit name for API adapters that call this operation "authorize".
    authorize_upload = initiate_upload

    def confirm_upload(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        document_id: str,
        *,
        correlation_id: str | None = None,
    ) -> Document:
        """Confirm only the server-owned object for an authorized document."""

        context = build_request_context(
            identity,
            requested_matter_id,
            self.authorization_store,
            correlation_id=correlation_id,
        )
        document = self.metadata_repository.get_for_scope(
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            document_id=document_id,
        )
        if document is None:
            raise DocumentError("document not found")
        upload_key = document.quarantine_s3_key or document.s3_key
        if document.status is DocumentStatus.FAILED:
            raise DocumentValidationError("document is not pending upload")
        if document.status is DocumentStatus.UPLOADED:
            validate_uploaded_object(self.object_storage, document)
            return document
        if document.status is not DocumentStatus.PENDING_UPLOAD:
            raise DocumentValidationError("document is not pending upload")
        validate_uploaded_object(self.object_storage, document, key=upload_key)
        if self.require_malware_scan:
            # The object is structurally valid but remains non-indexable until
            # the server-side GuardDuty result handler records a clean verdict.
            return document
        write_bedrock_metadata_sidecar(self.object_storage, document)
        promoted = self.metadata_repository.update_status(
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            document_id=document.document_id,
            status=DocumentStatus.UPLOADED,
        )
        if promoted.malware_scan_status is not MalwareScanStatus.CLEAN:
            promoted = replace(promoted, malware_scan_status=MalwareScanStatus.CLEAN)
            self.metadata_repository.save(promoted)
        return promoted

    def upload(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        request: UploadRequest,
        *,
        correlation_id: str | None = None,
    ) -> Document:
        """Local/direct-upload convenience implemented through the same lifecycle."""

        direct_request = request
        if (
            direct_request.file_size_bytes is None
            and isinstance(direct_request.body, bytes)
        ):
            direct_request = replace(
                direct_request, file_size_bytes=len(direct_request.body)
            )
        authorization = self.initiate_upload(
            identity, requested_matter_id, direct_request, correlation_id=correlation_id
        )
        document = authorization.document
        try:
            validate_upload(direct_request)
        except DocumentValidationError:
            self._best_effort_failed_metadata(document)
            raise
        try:
            self.object_storage.put_object(
                key=authorization.s3_key,
                body=direct_request.body,
                media_type=direct_request.media_type,
                metadata=_safe_metadata(document),
            )
        except Exception as exc:  # adapters normalize provider failures here
            self._best_effort_failed_metadata(document)
            raise DocumentStorageError("document object storage failed") from exc
        return self.confirm_upload(
            identity,
            requested_matter_id,
            document.document_id,
            correlation_id=correlation_id,
        )

    def mark_status(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        document_id: str,
        status: DocumentStatus,
        *,
        correlation_id: str | None = None,
    ) -> Document:
        """Reject the old generic transition endpoint.

        Upload completion is intentionally only reachable through
        :meth:`confirm_upload`; future processing transitions will be owned by
        their respective workers rather than by a client-selected status.
        """

        context = build_request_context(
            identity,
            requested_matter_id,
            self.authorization_store,
            correlation_id=correlation_id,
        )
        del context, document_id, status
        raise DocumentValidationError("generic document status transitions are not available")

    def list_documents(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        *,
        correlation_id: str | None = None,
    ) -> Sequence[Document]:
        context = build_request_context(
            identity,
            requested_matter_id,
            self.authorization_store,
            correlation_id=correlation_id,
        )
        return self.metadata_repository.list_for_scope(
            tenant_id=context.tenant_id, matter_id=context.matter_id
        )

    def _best_effort_failed_metadata(self, document: Document) -> None:
        try:
            self.metadata_repository.save(replace(document, status=DocumentStatus.FAILED))
        except Exception:
            # The original storage error is the useful public failure.  A
            # failing metadata store must not leak provider details.
            pass


_ALLOWED_STATUS_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING_UPLOAD: frozenset(
        {DocumentStatus.UPLOADED, DocumentStatus.FAILED}
    ),
    DocumentStatus.UPLOADED: frozenset(
        {DocumentStatus.PENDING_INGESTION, DocumentStatus.FAILED}
    ),
    DocumentStatus.PENDING_INGESTION: frozenset(
        {DocumentStatus.INDEXED, DocumentStatus.FAILED}
    ),
    DocumentStatus.INDEXED: frozenset({DocumentStatus.FAILED}),
    DocumentStatus.FAILED: frozenset({DocumentStatus.PENDING_INGESTION}),
}


@dataclass(slots=True)
class InMemoryObjectStorage:
    objects: dict[str, bytes]
    metadata: dict[str, dict[str, str]]
    tags: dict[str, dict[str, str]]
    fail: bool = False
    fail_delete: bool = False
    presigned_urls: dict[str, str] = field(default_factory=dict)
    presigned_content_lengths: dict[str, int] = field(default_factory=dict)

    def __init__(self, *, fail: bool = False, fail_delete: bool = False) -> None:
        self.objects = {}
        self.metadata = {}
        self.tags = {}
        self.fail = fail
        self.fail_delete = fail_delete
        self.presigned_urls = {}
        self.presigned_content_lengths = {}

    def put_object(
        self,
        *,
        key: str,
        body: bytes,
        media_type: str,
        metadata: Mapping[str, str],
    ) -> None:
        if self.fail:
            raise RuntimeError("fictional storage failure")
        expected_length = self.presigned_content_lengths.get(key)
        if expected_length is not None and len(body) != expected_length:
            raise DocumentStorageError("presigned upload content length mismatch")
        self.objects[key] = body
        self.metadata[key] = dict(metadata) | {"media-type": media_type}

    def delete_object(self, *, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("fictional cleanup failure")
        self.objects.pop(key, None)
        self.metadata.pop(key, None)
        self.tags.pop(key, None)
        self.presigned_urls.pop(key, None)
        self.presigned_content_lengths.pop(key, None)

    def copy_object(self, *, source_key: str, destination_key: str) -> None:
        if self.fail or source_key not in self.objects:
            raise RuntimeError("fictional storage copy failure")
        self.objects[destination_key] = self.objects[source_key]
        self.metadata[destination_key] = dict(self.metadata[source_key])
        # Match the production adapter: quarantine scan tags are not copied to
        # the canonical/indexable object.
        self.tags.pop(destination_key, None)

    def get_object_tagging(self, *, key: str) -> Mapping[str, str]:
        if key not in self.objects:
            raise KeyError(key)
        return dict(self.tags.get(key, {}))

    def set_object_tags(self, *, key: str, tags: Mapping[str, str]) -> None:
        if key not in self.objects:
            raise KeyError(key)
        self.tags[key] = dict(tags)

    def generate_presigned_put_url(
        self,
        *,
        key: str,
        content_length: int,
        media_type: str,
        metadata: Mapping[str, str],
        expires_in: int,
    ) -> str:
        if self.fail:
            raise RuntimeError("fictional storage failure")
        if (
            not isinstance(content_length, int)
            or isinstance(content_length, bool)
            or content_length <= 0
            or content_length > MAX_DOCUMENT_BYTES
        ):
            raise DocumentValidationError("presigned content length is invalid")
        url = f"https://s3.invalid/upload/{quote(key, safe='')}?expires={expires_in}"
        self.presigned_urls[key] = url
        self.presigned_content_lengths[key] = content_length
        return url

    def head_object(self, *, key: str) -> Mapping[str, Any]:
        if self.fail:
            raise RuntimeError("fictional storage failure")
        if key not in self.objects:
            raise KeyError(key)
        return {
            "ContentLength": len(self.objects[key]),
            "ContentType": self.metadata.get(key, {}).get("media-type"),
            "Metadata": {
                name: value
                for name, value in self.metadata.get(key, {}).items()
                if name != "media-type"
            },
        }

    def read_object_bytes(self, *, key: str, max_bytes: int) -> bytes:
        if self.fail:
            raise RuntimeError("fictional storage failure")
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
            or max_bytes > MAX_DOCUMENT_BYTES
        ):
            raise DocumentStorageError("object read limit is invalid")
        if key not in self.objects:
            raise KeyError(key)
        body = self.objects[key]
        if len(body) > max_bytes:
            raise DocumentStorageError("object content exceeds the read limit")
        return body


@dataclass(slots=True)
class InMemoryDocumentMetadataRepository:
    documents: dict[tuple[str, str, str], Document]
    fail: bool = False

    def __init__(self, *, fail: bool = False) -> None:
        self.documents = {}
        self.fail = fail

    def save(self, document: Document) -> None:
        if self.fail:
            raise RuntimeError("fictional metadata failure")
        self.documents[(document.tenant_id, document.matter_id, document.document_id)] = document

    def update_status(
        self, *, tenant_id: str, matter_id: str, document_id: str, status: DocumentStatus
    ) -> Document:
        key = (tenant_id, matter_id, document_id)
        document = self.documents.get(key)
        if document is None:
            raise DocumentError("document not found")
        if status not in _ALLOWED_STATUS_TRANSITIONS[document.status] and status is not document.status:
            raise DocumentValidationError("invalid document status transition")
        updated = replace(document, status=status)
        self.documents[key] = updated
        return updated

    def apply_malware_scan_result(
        self,
        *,
        tenant_id: str,
        matter_id: str,
        document_id: str,
        result_status: MalwareScanStatus,
        etag: str | None,
        version_id: str | None,
    ) -> Document:
        if self.fail:
            raise RuntimeError("fictional metadata failure")
        key = (tenant_id, matter_id, document_id)
        current = self.documents.get(key)
        if current is None:
            raise DocumentConcurrencyError("document transition conflict")
        target_status = (
            DocumentStatus.UPLOADED
            if result_status is MalwareScanStatus.CLEAN
            else DocumentStatus.FAILED
        )
        if current.status is target_status and current.malware_scan_status is result_status:
            if current.malware_scan_etag == etag and current.malware_scan_version_id == version_id:
                return current
            raise DocumentConcurrencyError("document transition conflict")
        if current.status is not DocumentStatus.PENDING_UPLOAD or current.malware_scan_status is not MalwareScanStatus.PENDING:
            raise DocumentConcurrencyError("document transition conflict")
        updated = replace(
            current,
            status=target_status,
            malware_scan_status=result_status,
            malware_scan_etag=etag,
            malware_scan_version_id=version_id,
        )
        self.documents[key] = updated
        return updated

    def get_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> Document | None:
        return self.documents.get((tenant_id, matter_id, document_id))

    def list_for_scope(
        self, *, tenant_id: str, matter_id: str, limit: int | None = None
    ) -> tuple[Document, ...]:
        documents = tuple(
            document
            for (stored_tenant, stored_matter, _), document in self.documents.items()
            if stored_tenant == tenant_id and stored_matter == matter_id
        )
        return documents if limit is None else documents[:limit]

    def list_for_scope_page(
        self, *, tenant_id: str, matter_id: str, limit: int, cursor: str | None = None
    ) -> tuple[tuple[Document, ...], str | None]:
        values = sorted(self.list_for_scope(tenant_id=tenant_id, matter_id=matter_id), key=lambda item: item.document_id)
        if cursor is not None:
            values = [item for item in values if item.document_id > cursor]
        page = tuple(values[:limit])
        return page, (page[-1].document_id if page and len(values) > len(page) else None)

    def delete_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> None:
        if self.fail:
            raise DocumentMetadataError("fictional metadata failure")
        self.documents.pop((tenant_id, matter_id, document_id), None)


class Boto3S3ObjectStorage:
    """Small boto3 adapter; boto3 is imported only when this adapter is used."""

    def __init__(self, bucket_name: str, *, client: Any | None = None) -> None:
        self.bucket_name = bucket_name
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                config=Config(
                    signature_version="s3v4",
                    retries={"total_max_attempts": 1, "mode": "standard"},
                ),
            )
        self.client = client

    def put_object(
        self,
        *,
        key: str,
        body: bytes,
        media_type: str,
        metadata: Mapping[str, str],
    ) -> None:
        self.client.put_object(
            Bucket=self.bucket_name,
            Key=key,
            Body=body,
            ContentType=media_type,
            Metadata=dict(metadata),
            ServerSideEncryption="AES256",
        )

    def delete_object(self, *, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket_name, Key=key)

    def copy_object(self, *, source_key: str, destination_key: str) -> None:
        self.client.copy_object(
            Bucket=self.bucket_name,
            CopySource={"Bucket": self.bucket_name, "Key": source_key},
            Key=destination_key,
            MetadataDirective="COPY",
            # GuardDuty writes its scan verdict on the quarantine object.  Do
            # not copy that trust signal into the canonical/indexable prefix.
            TaggingDirective="REPLACE",
            Tagging="",
            ServerSideEncryption="AES256",
        )

    def generate_presigned_put_url(
        self,
        *,
        key: str,
        content_length: int,
        media_type: str,
        metadata: Mapping[str, str],
        expires_in: int,
    ) -> str:
        if (
            not isinstance(content_length, int)
            or isinstance(content_length, bool)
            or content_length <= 0
            or content_length > MAX_DOCUMENT_BYTES
        ):
            raise DocumentValidationError("presigned content length is invalid")
        return self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": key,
                "ContentLength": content_length,
                "ContentType": media_type,
                "Metadata": dict(metadata),
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )

    def head_object(self, *, key: str) -> Mapping[str, Any]:
        return self.client.head_object(Bucket=self.bucket_name, Key=key)

    def read_object_bytes(self, *, key: str, max_bytes: int) -> bytes:
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
            or max_bytes > MAX_DOCUMENT_BYTES
        ):
            raise DocumentStorageError("object read limit is invalid")
        try:
            response = self.client.get_object(
                Bucket=self.bucket_name,
                Key=key,
                Range=f"bytes=0-{max_bytes - 1}",
            )
            body_stream = response.get("Body") if isinstance(response, Mapping) else None
            if body_stream is None or not hasattr(body_stream, "read"):
                raise DocumentStorageError("object body is invalid")
            body = body_stream.read(max_bytes)
            if not isinstance(body, bytes) or len(body) > max_bytes:
                raise DocumentStorageError("object body is invalid")
            return body
        except DocumentStorageError:
            raise
        except Exception as exc:
            raise DocumentStorageError("object content could not be read") from exc

    def get_object_tagging(self, *, key: str) -> Mapping[str, str]:
        response = self.client.get_object_tagging(Bucket=self.bucket_name, Key=key)
        if not isinstance(response, Mapping) or not isinstance(response.get("TagSet"), list):
            raise DocumentStorageError("object tags are invalid")
        result: dict[str, str] = {}
        for item in response["TagSet"]:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("Key"), str)
                or not isinstance(item.get("Value"), str)
                or item["Key"] in result
            ):
                raise DocumentStorageError("object tags are invalid")
            result[item["Key"]] = item["Value"]
        return result


class Boto3DynamoDocumentMetadataRepository:
    """DynamoDB single-table adapter storing metadata only."""

    def __init__(
        self,
        table_name: str,
        *,
        table: Any | None = None,
        boto3_backed: bool = False,
    ) -> None:
        self.table_name = table_name
        # An injected Table may still be a real boto3 resource (for example,
        # when auth and metadata repositories share one table). Keep the
        # backend choice explicit so dependency-free fakes retain their tuple
        # assertions while production callers can select boto3 conditions.
        self._boto3_backed = table is None or boto3_backed
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    @staticmethod
    def _item(document: Document) -> dict[str, Any]:
        return {
            "pk": document_partition_key(document.tenant_id, document.matter_id),
            "sk": document_sort_key(document.document_id),
            "entityType": "Document",
            "tenantId": document.tenant_id,
            "matterId": document.matter_id,
            "documentId": document.document_id,
            "name": document.name,
            "s3Key": document.s3_key,
            "quarantineS3Key": document.quarantine_s3_key,
            "mediaType": document.media_type,
            "jurisdiction": document.jurisdiction,
            "documentDate": document.document_date,
            "confidentiality": document.confidentiality,
            "status": document.status.value,
            "malwareScanStatus": document.malware_scan_status.value,
            "malwareScanETag": document.malware_scan_etag,
            "malwareScanVersionId": document.malware_scan_version_id,
            "fileSizeBytes": document.file_size_bytes,
            "uploadedAt": document.uploaded_at.isoformat(),
        }

    def save(self, document: Document) -> None:
        self.table.put_item(Item=self._item(document))

    def update_status(
        self, *, tenant_id: str, matter_id: str, document_id: str, status: DocumentStatus
    ) -> Document:
        response = self.table.get_item(
            Key={"pk": document_partition_key(tenant_id, matter_id), "sk": document_sort_key(document_id)}
        )
        item = response.get("Item")
        if not item:
            raise DocumentError("document not found")
        current = DocumentStatus(item["status"])
        if status not in _ALLOWED_STATUS_TRANSITIONS[current] and status is not current:
            raise DocumentValidationError("invalid document status transition")
        self.table.update_item(
            Key={"pk": document_partition_key(tenant_id, matter_id), "sk": document_sort_key(document_id)},
            UpdateExpression="SET #status = :status",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status.value,
                ":expected_status": current.value,
            },
            ConditionExpression="#status = :expected_status",
        )
        return _document_from_item({**item, "status": status.value})

    def apply_malware_scan_result(
        self,
        *,
        tenant_id: str,
        matter_id: str,
        document_id: str,
        result_status: MalwareScanStatus,
        etag: str | None,
        version_id: str | None,
    ) -> Document:
        key = {"pk": document_partition_key(tenant_id, matter_id), "sk": document_sort_key(document_id)}
        response = self.table.get_item(Key=key, ConsistentRead=True)
        item = response.get("Item")
        if not item:
            raise DocumentConcurrencyError("document transition conflict")
        current = _document_from_item(item)
        target_status = DocumentStatus.UPLOADED if result_status is MalwareScanStatus.CLEAN else DocumentStatus.FAILED
        if current.status is target_status and current.malware_scan_status is result_status:
            if current.malware_scan_etag == etag and current.malware_scan_version_id == version_id:
                return current
            raise DocumentConcurrencyError("document transition conflict")
        if current.status is not DocumentStatus.PENDING_UPLOAD or current.malware_scan_status is not MalwareScanStatus.PENDING:
            raise DocumentConcurrencyError("document transition conflict")
        names = {"#status": "status", "#scan": "malwareScanStatus"}
        values: dict[str, Any] = {
            ":status": target_status.value,
            ":scan": result_status.value,
            ":expected_status": DocumentStatus.PENDING_UPLOAD.value,
            ":expected_scan": MalwareScanStatus.PENDING.value,
        }
        set_parts = ["#status = :status", "#scan = :scan"]
        remove_parts: list[str] = []
        if etag is None:
            names["#etag"] = "malwareScanETag"
            remove_parts.append("#etag")
        else:
            names["#etag"] = "malwareScanETag"
            values[":etag"] = etag
            set_parts.append("#etag = :etag")
        if version_id is None:
            names["#version"] = "malwareScanVersionId"
            remove_parts.append("#version")
        else:
            names["#version"] = "malwareScanVersionId"
            values[":version"] = version_id
            set_parts.append("#version = :version")
        expression = "SET " + ", ".join(set_parts)
        if remove_parts:
            expression += " REMOVE " + ", ".join(remove_parts)
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="#status = :expected_status AND #scan = :expected_scan",
            )
        except Exception as exc:
            raise DocumentConcurrencyError("document transition conflict") from exc
        return replace(
            current,
            status=target_status,
            malware_scan_status=result_status,
            malware_scan_etag=etag,
            malware_scan_version_id=version_id,
        )

    def get_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> Document | None:
        response = self.table.get_item(
            Key={
                "pk": document_partition_key(tenant_id, matter_id),
                "sk": document_sort_key(document_id),
            },
            ConsistentRead=True,
        )
        item = response.get("Item")
        return _document_from_item(item) if item else None

    def list_for_scope(
        self, *, tenant_id: str, matter_id: str, limit: int | None = None
    ) -> tuple[Document, ...]:
        documents: list[Document] = []
        if self._boto3_backed:
            from boto3.dynamodb.conditions import Key

            key_condition: Any = Key("pk").eq(
                document_partition_key(tenant_id, matter_id)
            ) & Key("sk").begins_with("DOCUMENT#")
        else:
            # Dependency-free injected fakes can still assert the exact
            # partition and prefix contract without importing boto3.
            key_condition = (
                "pk",
                document_partition_key(tenant_id, matter_id),
                "sk begins_with",
                "DOCUMENT#",
            )
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": key_condition,
            "ConsistentRead": True,
        }
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                raise ValueError("document query limit must be positive")
            query_kwargs["Limit"] = limit
        while True:
            response = self.table.query(**query_kwargs)
            documents.extend(_document_from_item(item) for item in response.get("Items", ()))
            if limit is not None and len(documents) >= limit:
                return tuple(documents[:limit])
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        return tuple(documents)

    def list_for_scope_page(
        self, *, tenant_id: str, matter_id: str, limit: int, cursor: str | None = None
    ) -> tuple[tuple[Document, ...], str | None]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("document query limit must be positive")
        from boto3.dynamodb.conditions import Key
        key_condition: Any = Key("pk").eq(document_partition_key(tenant_id, matter_id)) & Key("sk").begins_with("DOCUMENT#")
        kwargs: dict[str, Any] = {"KeyConditionExpression": key_condition, "ConsistentRead": True, "Limit": limit}
        if cursor:
            kwargs["ExclusiveStartKey"] = {"pk": document_partition_key(tenant_id, matter_id), "sk": document_sort_key(cursor)}
        response = self.table.query(**kwargs)
        documents = tuple(_document_from_item(item) for item in response.get("Items", ()))
        last = response.get("LastEvaluatedKey")
        next_cursor = None
        if isinstance(last, Mapping) and isinstance(last.get("sk"), str) and last["sk"].startswith("DOCUMENT#"):
            next_cursor = last["sk"][len("DOCUMENT#"):]
        return documents, next_cursor

    def delete_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> None:
        key = {
            "pk": document_partition_key(tenant_id, matter_id),
            "sk": document_sort_key(document_id),
        }
        try:
            self.table.delete_item(
                Key=key,
                ConditionExpression="#entity = :entity AND #tenant = :tenant AND #matter = :matter AND #document = :document",
                ExpressionAttributeNames={
                    "#entity": "entityType",
                    "#tenant": "tenantId",
                    "#matter": "matterId",
                    "#document": "documentId",
                },
                ExpressionAttributeValues={
                    ":entity": "Document",
                    ":tenant": tenant_id,
                    ":matter": matter_id,
                    ":document": document_id,
                },
            )
        except Exception as exc:
            # A concurrent/idempotent retry may already have removed it.  A
            # point read distinguishes that safe outcome from a live failure.
            if self.get_for_scope(
                tenant_id=tenant_id, matter_id=matter_id, document_id=document_id
            ) is not None:
                raise DocumentMetadataError("document metadata deletion failed") from exc


def _document_from_item(item: Mapping[str, Any]) -> Document:
    return Document(
        document_id=str(item["documentId"]),
        matter_id=str(item["matterId"]),
        tenant_id=str(item["tenantId"]),
        name=str(item["name"]),
        s3_key=str(item["s3Key"]),
        quarantine_s3_key=item.get("quarantineS3Key") if isinstance(item.get("quarantineS3Key"), str) else None,
        media_type=str(item["mediaType"]),
        jurisdiction=str(item["jurisdiction"]),
        document_date=str(item["documentDate"]),
        confidentiality=str(item["confidentiality"]),
        status=DocumentStatus(item["status"]),
        malware_scan_status=MalwareScanStatus(item.get("malwareScanStatus", MalwareScanStatus.PENDING.value)),
        malware_scan_etag=item.get("malwareScanETag") if isinstance(item.get("malwareScanETag"), str) else None,
        malware_scan_version_id=item.get("malwareScanVersionId") if isinstance(item.get("malwareScanVersionId"), str) else None,
        file_size_bytes=int(item.get("fileSizeBytes", 0)),
        uploaded_at=datetime.fromisoformat(str(item["uploadedAt"])),
    )
