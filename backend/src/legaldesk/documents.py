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
)
from .domain.models import Document, DocumentStatus, utc_now


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
ALLOWED_MEDIA_TYPES = frozenset({"application/pdf", "text/plain"})
ALLOWED_CONFIDENTIALITY = frozenset({"public-fictional", "fictional-internal"})
MAX_FILENAME_LENGTH = 255
MAX_METADATA_TEXT_LENGTH = 256
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

    def generate_presigned_put_url(
        self,
        *,
        key: str,
        media_type: str,
        metadata: Mapping[str, str],
        expires_in: int,
    ) -> str: ...

    def head_object(self, *, key: str) -> Mapping[str, Any]: ...


class DocumentMetadataRepository(Protocol):
    def save(self, document: Document) -> None: ...

    def update_status(
        self, *, tenant_id: str, matter_id: str, document_id: str, status: DocumentStatus
    ) -> Document: ...

    def get_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> Document | None: ...

    def list_for_scope(self, *, tenant_id: str, matter_id: str) -> Sequence[Document]: ...


def document_partition_key(tenant_id: str, matter_id: str) -> str:
    return f"TENANT#{tenant_id}#MATTER#{matter_id}"


def document_sort_key(document_id: str) -> str:
    return f"DOCUMENT#{document_id}"


def build_document_key(
    context: RequestContext, document_id: str, media_type: str
) -> str:
    """Build an object key solely from server-derived scope and ID."""

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


def build_bedrock_metadata_sidecar(document: Document) -> bytes:
    """Serialize filterable Bedrock metadata without embedding it as content."""

    import json

    attributes = {
        "tenantId": document.tenant_id,
        "matterId": document.matter_id,
        "documentId": document.document_id,
        "mediaType": document.media_type,
        "jurisdiction": document.jurisdiction,
        "confidentiality": document.confidentiality,
    }
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
        return self.document.s3_key

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
        )
        try:
            self.metadata_repository.save(document)
        except Exception as exc:
            raise DocumentMetadataError("document metadata persistence failed") from exc
        try:
            upload_url = self.object_storage.generate_presigned_put_url(
                key=key,
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
        if document.status is not DocumentStatus.PENDING_UPLOAD:
            raise DocumentValidationError("document is not pending upload")
        try:
            head = self.object_storage.head_object(key=document.s3_key)
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
        sidecar = build_bedrock_metadata_sidecar(document)
        try:
            self.object_storage.put_object(
                key=f"{document.s3_key}.metadata.json",
                body=sidecar,
                media_type="application/json",
                metadata={"document-id": document.document_id},
            )
        except Exception as exc:
            raise DocumentStorageError("document metadata sidecar could not be stored") from exc
        return self.metadata_repository.update_status(
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            document_id=document.document_id,
            status=DocumentStatus.UPLOADED,
        )

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
                key=document.s3_key,
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
    fail: bool = False
    fail_delete: bool = False
    presigned_urls: dict[str, str] = field(default_factory=dict)

    def __init__(self, *, fail: bool = False, fail_delete: bool = False) -> None:
        self.objects = {}
        self.metadata = {}
        self.fail = fail
        self.fail_delete = fail_delete
        self.presigned_urls = {}

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
        self.objects[key] = body
        self.metadata[key] = dict(metadata) | {"media-type": media_type}

    def delete_object(self, *, key: str) -> None:
        if self.fail_delete:
            raise RuntimeError("fictional cleanup failure")
        self.objects.pop(key, None)
        self.metadata.pop(key, None)

    def generate_presigned_put_url(
        self,
        *,
        key: str,
        media_type: str,
        metadata: Mapping[str, str],
        expires_in: int,
    ) -> str:
        if self.fail:
            raise RuntimeError("fictional storage failure")
        url = f"https://s3.invalid/upload/{quote(key, safe='')}?expires={expires_in}"
        self.presigned_urls[key] = url
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

    def get_for_scope(
        self, *, tenant_id: str, matter_id: str, document_id: str
    ) -> Document | None:
        return self.documents.get((tenant_id, matter_id, document_id))

    def list_for_scope(self, *, tenant_id: str, matter_id: str) -> tuple[Document, ...]:
        return tuple(
            document
            for (stored_tenant, stored_matter, _), document in self.documents.items()
            if stored_tenant == tenant_id and stored_matter == matter_id
        )


class Boto3S3ObjectStorage:
    """Small boto3 adapter; boto3 is imported only when this adapter is used."""

    def __init__(self, bucket_name: str, *, client: Any | None = None) -> None:
        self.bucket_name = bucket_name
        if client is None:
            import boto3

            client = boto3.client("s3")
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

    def generate_presigned_put_url(
        self,
        *,
        key: str,
        media_type: str,
        metadata: Mapping[str, str],
        expires_in: int,
    ) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": key,
                "ContentType": media_type,
                "Metadata": dict(metadata),
                "ServerSideEncryption": "AES256",
            },
            ExpiresIn=expires_in,
            HttpMethod="PUT",
        )

    def head_object(self, *, key: str) -> Mapping[str, Any]:
        return self.client.head_object(Bucket=self.bucket_name, Key=key)


class Boto3DynamoDocumentMetadataRepository:
    """DynamoDB single-table adapter storing metadata only."""

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        self.table_name = table_name
        self._boto3_backed = table is None
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
            "mediaType": document.media_type,
            "jurisdiction": document.jurisdiction,
            "documentDate": document.document_date,
            "confidentiality": document.confidentiality,
            "status": document.status.value,
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

    def list_for_scope(self, *, tenant_id: str, matter_id: str) -> tuple[Document, ...]:
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
        while True:
            response = self.table.query(**query_kwargs)
            documents.extend(_document_from_item(item) for item in response.get("Items", ()))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_kwargs["ExclusiveStartKey"] = last_key
        return tuple(documents)


def _document_from_item(item: Mapping[str, Any]) -> Document:
    return Document(
        document_id=str(item["documentId"]),
        matter_id=str(item["matterId"]),
        tenant_id=str(item["tenantId"]),
        name=str(item["name"]),
        s3_key=str(item["s3Key"]),
        media_type=str(item["mediaType"]),
        jurisdiction=str(item["jurisdiction"]),
        document_date=str(item["documentDate"]),
        confidentiality=str(item["confidentiality"]),
        status=DocumentStatus(item["status"]),
        file_size_bytes=int(item.get("fileSizeBytes", 0)),
        uploaded_at=datetime.fromisoformat(str(item["uploadedAt"])),
    )
