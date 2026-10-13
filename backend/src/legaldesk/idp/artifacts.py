"""Immutable IDP-only source/text/model artifact storage.

Artifacts are deliberately outside the existing ``tenants/`` RAG prefixes and
are addressed by server-owned scope plus content hash.  Reads re-check the
hash, so a stale or replaced object cannot be consumed as the current run.
"""

from __future__ import annotations

import hashlib
import re
from io import BytesIO
from typing import Any, Protocol

from .models import IDPContractError

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024


class IDPArtifactError(IDPContractError):
    pass


def idp_artifact_key(*, tenant_id: str, matter_id: str, document_id: str, run_id: str, kind: str, sha256: str) -> str:
    values = (tenant_id, matter_id, document_id, run_id, kind, sha256)
    if any(not isinstance(value, str) or _SAFE.fullmatch(value) is None for value in values[:5]) or _SHA.fullmatch(sha256) is None:
        raise IDPArtifactError("artifact scope or hash is invalid")
    return f"idp-artifacts/tenant={tenant_id}/matter={matter_id}/document={document_id}/run={run_id}/{kind}-{sha256}"


class IDPArtifactStore(Protocol):
    def put_immutable(self, *, key: str, body: bytes, media_type: str, sha256: str) -> str: ...

    def read_verified(self, *, key: str, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes: ...


class InMemoryIDPArtifactStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str, str]] = {}

    def put_immutable(self, *, key: str, body: bytes, media_type: str, sha256: str) -> str:
        if hashlib.sha256(body).hexdigest() != sha256:
            raise IDPArtifactError("artifact content hash mismatch")
        old = self.objects.get(key)
        if old is not None and old[2] != sha256:
            raise IDPArtifactError("immutable artifact key collision")
        self.objects.setdefault(key, (body, media_type, sha256))
        return key

    def read_verified(self, *, key: str, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
        item = self.objects.get(key)
        bound_hash = key.rsplit("-", 1)[-1] if isinstance(key, str) else ""
        if item is None or not _SHA.fullmatch(bound_hash) or item[2] != bound_hash or len(item[0]) > max_bytes or hashlib.sha256(item[0]).hexdigest() != item[2]:
            raise IDPArtifactError("artifact is unavailable or failed verification")
        return item[0]


class Boto3S3IDPArtifactStore:
    def __init__(self, bucket_name: str, *, client: Any) -> None:
        if not isinstance(bucket_name, str) or not bucket_name.strip() or client is None:
            raise IDPArtifactError("IDP artifact storage configuration is invalid")
        self.bucket_name = bucket_name
        self.client = client

    def put_immutable(self, *, key: str, body: bytes, media_type: str, sha256: str) -> str:
        self._validate_key(key, sha256)
        if len(body) > MAX_ARTIFACT_BYTES or hashlib.sha256(body).hexdigest() != sha256:
            raise IDPArtifactError("artifact content is invalid")
        try:
            self.client.put_object(
                Bucket=self.bucket_name, Key=key, Body=body, ContentType=media_type,
                Metadata={"legaldesk-sha256": sha256}, ServerSideEncryption="AES256", IfNoneMatch="*",
            )
        except Exception as exc:
            # A precondition failure is safe only if the existing object proves
            # the same bytes, not merely copied metadata.  A timeout or an
            # already-existing object is ambiguous until the bounded read is
            # verified end-to-end.
            try:
                head = self.client.head_object(Bucket=self.bucket_name, Key=key)
                metadata = head.get("Metadata", {}) if isinstance(head, dict) else {}
                content_length = head.get("ContentLength") if isinstance(head, dict) else None
                if metadata.get("legaldesk-sha256") != sha256 or content_length != len(body):
                    raise IDPArtifactError("immutable artifact collision") from exc
                existing = self.client.get_object(Bucket=self.bucket_name, Key=key, Range=f"bytes=0-{len(body)}")
                stream = existing.get("Body") if isinstance(existing, dict) else None
                observed = stream.read(len(body) + 1) if stream is not None and hasattr(stream, "read") else None
                if not isinstance(observed, bytes) or len(observed) != len(body) or observed != body or hashlib.sha256(observed).hexdigest() != sha256:
                    raise IDPArtifactError("immutable artifact collision") from exc
            except IDPArtifactError:
                raise
            except Exception:
                raise IDPArtifactError("immutable artifact write failed") from None
        return key

    @staticmethod
    def _validate_key(key: str, sha256: str) -> None:
        if not isinstance(key, str) or not key.startswith("idp-artifacts/") or ".." in key or not _SHA.fullmatch(sha256) or not key.endswith(f"-{sha256}"):
            raise IDPArtifactError("artifact key is outside the IDP scope")

    def read_verified(self, *, key: str, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
        if not isinstance(key, str) or not key.startswith("idp-artifacts/") or ".." in key:
            raise IDPArtifactError("artifact key is outside the IDP scope")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_ARTIFACT_BYTES:
            raise IDPArtifactError("artifact read bound is invalid")
        bound_hash = key.rsplit("-", 1)[-1]
        if _SHA.fullmatch(bound_hash) is None:
            raise IDPArtifactError("artifact key hash is invalid")
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=key, Range=f"bytes=0-{max_bytes - 1}")
            body = response.get("Body") if isinstance(response, dict) else None
            if body is None or not hasattr(body, "read"):
                raise IDPArtifactError("artifact body is invalid")
            content = body.read(max_bytes)
            metadata = response.get("Metadata", {})
            expected = metadata.get("legaldesk-sha256") if isinstance(metadata, dict) else None
            if not isinstance(content, bytes) or len(content) > max_bytes or expected != bound_hash or not isinstance(expected, str) or hashlib.sha256(content).hexdigest() != expected:
                raise IDPArtifactError("artifact failed hash verification")
            return content
        except IDPArtifactError:
            raise
        except Exception as exc:
            raise IDPArtifactError("artifact read failed") from exc


__all__ = ["Boto3S3IDPArtifactStore", "IDPArtifactError", "IDPArtifactStore", "InMemoryIDPArtifactStore", "MAX_ARTIFACT_BYTES", "idp_artifact_key"]
