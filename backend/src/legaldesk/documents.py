"""Authorized document upload and metadata boundaries.

The service in this module is deliberately HTTP-neutral.  An API adapter can
translate a multipart request into :class:`UploadRequest`, but it cannot
choose the effective tenant, matter, document ID, or object key.  Bodies go to
object storage only; the metadata repository receives identifiers and safe
metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import PurePath
from typing import Any, Callable, Mapping, Protocol, Sequence
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


class DocumentError(Exception):
    """Base class for expected document pipeline errors."""


class DocumentValidationError(DocumentError, ValueError):
    """The upload does not satisfy the local safety policy."""


class DocumentStorageError(DocumentError):
    """Object storage failed; callers must not treat the upload as complete."""


class DocumentMetadataError(DocumentError):
    """Metadata persistence failed after object storage completed."""


@dataclass(frozen=True, slots=True)
class UploadRequest:
    """Untrusted upload fields normalized at the service boundary."""

    filename: str
    media_type: str
    body: bytes
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


class DocumentMetadataRepository(Protocol):
    def save(self, document: Document) -> None: ...

    def update_status(
        self, *, tenant_id: str, matter_id: str, document_id: str, status: DocumentStatus
    ) -> Document: ...

    def list_for_scope(self, *, tenant_id: str, matter_id: str) -> Sequence[Document]: ...


def document_partition_key(tenant_id: str, matter_id: str) -> str:
    return f"TENANT#{tenant_id}#MATTER#{matter_id}"


def document_sort_key(document_id: str) -> str:
    return f"DOCUMENT#{document_id}"


def build_document_key(context: RequestContext, document_id: str) -> str:
    """Build an object key solely from server-derived scope and ID."""

    try:
        UUID(document_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("document_id must be a UUID") from exc
    return (
        f"tenants/{context.tenant_id}/matters/{context.matter_id}/"
        f"documents/{document_id}/original"
    )


def _validate_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DocumentValidationError(f"{field_name} is required")
    if len(value) > MAX_METADATA_TEXT_LENGTH:
        raise DocumentValidationError(f"{field_name} is too long")


def validate_upload(request: UploadRequest) -> None:
    if not isinstance(request.body, bytes):
        raise DocumentValidationError("body must be bytes")
    if not request.body or len(request.body) > MAX_DOCUMENT_BYTES:
        raise DocumentValidationError("body size is outside the allowed limit")
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
    if request.media_type == "application/pdf" and not request.body.startswith(b"%PDF-"):
        raise DocumentValidationError("PDF body has an invalid signature")
    if request.media_type == "text/plain":
        try:
            request.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DocumentValidationError("text body must be valid UTF-8") from exc
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


@dataclass(slots=True)
class DocumentPipeline:
    authorization_store: AuthorizationStore
    object_storage: ObjectStorage
    metadata_repository: DocumentMetadataRepository
    id_factory: Callable[[], UUID] = uuid4
    clock: Callable[[], datetime] = utc_now

    def upload(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        request: UploadRequest,
        *,
        correlation_id: str | None = None,
    ) -> Document:
        """Authorize, validate, store the body, and persist metadata.

        Authorization happens before validation and storage, so a denied
        cross-matter request has no observable storage side effect.
        """

        context = build_request_context(
            identity,
            requested_matter_id,
            self.authorization_store,
            correlation_id=correlation_id,
        )
        validate_upload(request)
        document_id = str(self.id_factory())
        key = build_document_key(context, document_id)
        uploaded_at = self.clock()
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
            status=DocumentStatus.UPLOADED,
            file_size_bytes=len(request.body),
            uploaded_at=uploaded_at,
        )
        try:
            self.object_storage.put_object(
                key=key,
                body=request.body,
                media_type=request.media_type,
                metadata=_safe_metadata(document),
            )
        except Exception as exc:  # adapters normalize provider failures here
            self._best_effort_failed_metadata(document)
            raise DocumentStorageError("document object storage failed") from exc
        try:
            self.metadata_repository.save(document)
        except Exception as exc:
            try:
                self.object_storage.delete_object(key=key)
            except Exception:
                # Preserve the stable public metadata error even if cleanup
                # itself fails; the orphan is observable only operationally.
                pass
            raise DocumentMetadataError("document metadata persistence failed") from exc
        return document

    def mark_status(
        self,
        identity: VerifiedIdentity,
        requested_matter_id: str,
        document_id: str,
        status: DocumentStatus,
        *,
        correlation_id: str | None = None,
    ) -> Document:
        context = build_request_context(
            identity,
            requested_matter_id,
            self.authorization_store,
            correlation_id=correlation_id,
        )
        try:
            status = DocumentStatus(status)
        except ValueError as exc:
            raise DocumentValidationError("unknown document status") from exc
        return self.metadata_repository.update_status(
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            document_id=document_id,
            status=status,
        )

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

    def __init__(self, *, fail: bool = False, fail_delete: bool = False) -> None:
        self.objects = {}
        self.metadata = {}
        self.fail = fail
        self.fail_delete = fail_delete

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
            ExpressionAttributeValues={":status": status.value},
        )
        return _document_from_item({**item, "status": status.value})

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
