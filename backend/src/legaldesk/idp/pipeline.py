"""Bounded IDP processing orchestration over injected providers and stores."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .acquisition import PDFTextDocument, acquire_pdf
from .models import DEFAULT_MAX_BYTES, DEFAULT_MAX_PAGES
from .artifacts import IDPArtifactStore, idp_artifact_key
from .models import DocumentType, EvidenceAnchor, FieldAcceptance, FieldOrigin, FieldPresence, IDPExtractionRun, IDPFieldResult, IDPJobStatus, IDP_SCHEMA_VERSION
from .processing import ClassificationResult, DocumentClassifier, DocumentFieldExtractor, ExtractionResult, IDPOutputError, PaidStage, StageCallLedger
from .registry import IDPSchemaRegistry


@dataclass(frozen=True, slots=True)
class IDPPipelineResult:
    status: IDPJobStatus
    classification: ClassificationResult | None = None
    run: IDPExtractionRun | None = None
    document: PDFTextDocument | None = None
    reason: str | None = None
    source_artifact_key: str | None = None


def _stage_artifact(*, store: IDPArtifactStore, tenant_id: str, matter_id: str, document_id: str, run_id: str, kind: str, payload: bytes) -> str:
    sha = hashlib.sha256(payload).hexdigest()
    key = idp_artifact_key(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id, kind=kind, sha256=sha)
    return store.put_immutable(key=key, body=payload, media_type="application/json", sha256=sha)


class IDPProcessingPipeline:
    """Digital path plus explicit OCR continuation result; no Lambda polling."""

    def __init__(self, *, classifier: DocumentClassifier, extractor: DocumentFieldExtractor, registry: IDPSchemaRegistry, artifact_store: IDPArtifactStore, stage_ledger: StageCallLedger, max_bytes: int = DEFAULT_MAX_BYTES, max_pages: int = DEFAULT_MAX_PAGES, max_calls_per_run: int = 2) -> None:
        self.classifier, self.extractor, self.registry = classifier, extractor, registry
        self.artifact_store, self.stage_ledger = artifact_store, stage_ledger
        if isinstance(max_calls_per_run, bool) or not isinstance(max_calls_per_run, int) or not 1 <= max_calls_per_run <= 8:
            raise ValueError("max_calls_per_run is invalid")
        self.max_bytes, self.max_pages, self.max_calls_per_run = max_bytes, max_pages, max_calls_per_run

    @staticmethod
    def _ensure_deadline(deadline_at: datetime | None, reserve_seconds: float = 0.0) -> None:
        if deadline_at is not None and (deadline_at - datetime.now(timezone.utc)).total_seconds() <= reserve_seconds:
            raise IDPOutputError("IDP global deadline exceeded")

    def process(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str, content: bytes, content_sha256: str, model_id: str, prompt_version: str, deadline_at: datetime | None = None, deadline_reserve_seconds: float = 0.0) -> IDPPipelineResult:
        self._ensure_deadline(deadline_at, deadline_reserve_seconds)
        source_key = idp_artifact_key(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id, kind="source", sha256=content_sha256)
        self.artifact_store.put_immutable(key=source_key, body=content, media_type="application/pdf", sha256=content_sha256)
        document = acquire_pdf(content, expected_sha256=content_sha256, max_bytes=self.max_bytes, max_pages=self.max_pages)
        if not document.coverage_complete:
            return IDPPipelineResult(IDPJobStatus.WAITING_FOR_OCR, document=document, reason="OCR_REQUIRED_FOR_PAGE_COVERAGE", source_artifact_key=source_key)
        result = self.process_pages(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id, pages=document.page_map(), content_sha256=content_sha256, model_id=model_id, prompt_version=prompt_version, document=document, deadline_at=deadline_at, deadline_reserve_seconds=deadline_reserve_seconds)
        return IDPPipelineResult(result.status, result.classification, result.run, result.document, result.reason, source_key)

    def process_pages(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str, pages: Mapping[int, str], content_sha256: str, model_id: str, prompt_version: str, document: PDFTextDocument | None = None, deadline_at: datetime | None = None, deadline_reserve_seconds: float = 0.0) -> IDPPipelineResult:
        """Continue from canonical digital/OCR page text without re-acquiring PDF."""

        self._ensure_deadline(deadline_at, deadline_reserve_seconds)
        classifier = self._classify(tenant_id, matter_id, document_id, run_id, pages, content_sha256, model_id=model_id, prompt_version=prompt_version, deadline_at=deadline_at, deadline_reserve_seconds=deadline_reserve_seconds)
        schema = self.registry.get(classifier.document_type)
        self._ensure_deadline(deadline_at, deadline_reserve_seconds)
        extracted = self._extract(tenant_id, matter_id, document_id, run_id, schema, pages, content_sha256, model_id=model_id, prompt_version=prompt_version, deadline_at=deadline_at, deadline_reserve_seconds=deadline_reserve_seconds)
        run = IDPExtractionRun(run_id=run_id, tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, document_sha256=content_sha256, document_type=schema.document_type, schema_version=schema.version, model_id=model_id, prompt_version=prompt_version, status=IDPJobStatus.COMPLETED, fields=extracted.fields)
        result_payload = json.dumps({"runId": run_id, "documentType": schema.document_type.value, "schemaVersion": schema.version, "fields": {name: {"presence": field.presence.value, "origin": field.origin.value, "acceptance": field.acceptance.value, "value": field.value, "reason": field.reason, "validation": dict(field.validation), "provenance": dict(field.provenance), "evidence": [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in field.evidence]} for name, field in extracted.fields.items()}}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        _stage_artifact(store=self.artifact_store, tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id, kind="run", payload=result_payload)
        return IDPPipelineResult(IDPJobStatus.COMPLETED, classifier, run, document)

    def _classify(self, tenant_id: str, matter_id: str, document_id: str, run_id: str, pages: Mapping[int, str], sha: str, *, model_id: str = "", prompt_version: str = "", deadline_at: datetime | None = None, deadline_reserve_seconds: float = 0.0) -> ClassificationResult:
        request_hash = self._request_hash(
            stage="CLASSIFIER", provider=self.classifier, sha=sha, pages=pages,
            fallback={"model": getattr(self.classifier, "model_id", model_id), "prompt": getattr(self.classifier, "prompt_version", prompt_version)},
        )
        existing = self.stage_ledger.get(run_id=run_id, stage=PaidStage.CLASSIFIER)
        if existing is None and self.stage_ledger.count(run_id=run_id) >= self.max_calls_per_run:
            raise IDPOutputError("IDP paid-call budget exhausted")
        record = self.stage_ledger.begin(run_id=run_id, stage=PaidStage.CLASSIFIER, request_hash=request_hash)
        if record.state.name == "COMMITTED":
            payload = json.loads(self.artifact_store.read_verified(key=record.artifact_ref))
            return ClassificationResult(
                DocumentType(payload["documentType"]), payload.get("subtype"),
                tuple(EvidenceAnchor(page=int(anchor["page"]), quote=str(anchor["quote"]), content_sha256=str(anchor["contentSha256"]), start=anchor.get("start"), end=anchor.get("end")) for anchor in payload.get("evidence", ())),
                str(payload["promptVersion"]), str(payload["modelId"]), FieldAcceptance(payload["acceptance"]), dict(payload.get("metadata", {})),
            )
        try:
            self._ensure_deadline(deadline_at, deadline_reserve_seconds)
            value = self.classifier.classify(page_text=pages, content_sha256=sha)
            payload = json.dumps({
                "documentType": value.document_type.value, "subtype": value.subtype,
                "promptVersion": value.prompt_version, "modelId": value.model_id,
                "acceptance": value.acceptance.value,
                "metadata": dict(value.metadata),
                "evidence": [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in value.evidence],
            }, separators=(",", ":")).encode()
            ref = _stage_artifact(store=self.artifact_store, tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id, kind="classifier", payload=payload)
            self.stage_ledger.commit(record, artifact_ref=ref)
            return value
        except Exception:
            self.stage_ledger.mark_ambiguous(record)
            raise

    def _extract(self, tenant_id: str, matter_id: str, document_id: str, run_id: str, schema, pages: Mapping[int, str], sha: str, *, model_id: str = "", prompt_version: str = "", deadline_at: datetime | None = None, deadline_reserve_seconds: float = 0.0):
        request_hash = self._request_hash(
            stage="EXTRACTOR", provider=self.extractor, sha=sha, pages=pages,
            fallback={"schema": schema.version, "model": getattr(self.extractor, "model_id", model_id), "prompt": getattr(self.extractor, "prompt_version", prompt_version)},
            schema=schema,
        )
        existing = self.stage_ledger.get(run_id=run_id, stage=PaidStage.EXTRACTOR)
        if existing is None and self.stage_ledger.count(run_id=run_id) >= self.max_calls_per_run:
            raise IDPOutputError("IDP paid-call budget exhausted")
        record = self.stage_ledger.begin(run_id=run_id, stage=PaidStage.EXTRACTOR, request_hash=request_hash)
        if record.state.name == "COMMITTED":
            payload = json.loads(self.artifact_store.read_verified(key=record.artifact_ref))
            fields = {
                name: IDPFieldResult(field=name, value=item.get("value"), presence=FieldPresence(item["presence"]), origin=FieldOrigin(item["origin"]), acceptance=FieldAcceptance(item["acceptance"]), evidence=tuple(EvidenceAnchor(page=int(anchor["page"]), quote=str(anchor["quote"]), content_sha256=str(anchor["contentSha256"]), start=anchor.get("start"), end=anchor.get("end")) for anchor in item.get("evidence", ())), validation={str(key): bool(value) for key, value in item.get("validation", {}).items()}, reason=item.get("reason"), schema_version=schema.version, provenance=dict(item.get("provenance", {})))
                for name, item in payload["fields"].items()
            }
            return ExtractionResult(schema.document_type, schema.version, fields, dict(payload.get("metadata", {})))
        try:
            self._ensure_deadline(deadline_at, deadline_reserve_seconds)
            value = self.extractor.extract(schema=schema, page_text=pages, content_sha256=sha)
            payload = json.dumps({"schema": schema.version, "metadata": dict(value.metadata), "fields": {name: {"value": field.value, "presence": field.presence.value, "origin": field.origin.value, "acceptance": field.acceptance.value, "reason": field.reason, "validation": dict(field.validation), "provenance": dict(field.provenance), "evidence": [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in field.evidence]} for name, field in value.fields.items()}}, separators=(",", ":")).encode()
            ref = _stage_artifact(store=self.artifact_store, tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=run_id, kind="extractor", payload=payload)
            self.stage_ledger.commit(record, artifact_ref=ref)
            return value
        except Exception:
            self.stage_ledger.mark_ambiguous(record)
            raise

    @staticmethod
    def _request_hash(*, stage: str, provider: object, sha: str, pages: Mapping[int, str], fallback: Mapping[str, object], schema: object | None = None) -> str:
        request_hash = getattr(provider, "request_hash", None)
        if callable(request_hash):
            kwargs: dict[str, object] = {"page_text": pages, "content_sha256": sha}
            if schema is not None:
                kwargs["schema"] = schema
            try:
                value = request_hash(**kwargs)
            except TypeError:
                value = None
            if isinstance(value, str) and value:
                return value
        payload = {"stage": stage, "sha": sha, "pages": pages, **fallback}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = ["IDPPipelineResult", "IDPProcessingPipeline"]
