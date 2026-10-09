"""Phase 14.1 IDP contracts, persistence and queue boundary."""

from .models import *
from .registry import DEFAULT_SCHEMAS, IDPFieldSpec, IDPSchema, IDPSchemaRegistry
from .persistence import (
    AuthoritativeJobLocator,
    Boto3DynamoIDPRepository,
    DeliveryCandidatePage,
    IDPIdempotencyConflict,
    IDPRepository,
    InMemoryIDPRepository,
    idempotency_sort_key,
    job_locator_partition_key,
    job_locator_sort_key,
    job_sort_key,
    matter_partition_key,
    run_sort_key,
)
from .worker import (
    IDPDocumentLookup,
    IDPPaidCallGate,
    IDPQueue,
    IDPSourceArnError,
    IDPTransientError,
    IDPWorker,
    IDPWorkerError,
    create_verified_clean_job,
    enqueue_verified_clean_job,
    idempotency_key,
    preflight_document,
    redeliver_ambiguous_job,
)
from .acquisition import (
    IDPPageText,
    MAX_PAGE_TEXT_CHARS,
    MAX_TOTAL_TEXT_CHARS,
    PDFAcquisitionError,
    PDFLimitExceeded,
    PDFTextDocument,
    acquire_pdf,
    normalize_page_text,
)
from .ocr import (
    InMemoryOCRJobStore,
    OCRCompletion,
    OCRCoordinator,
    OCRContractError,
    OCRJobRecord,
    OCRJobStore,
    OCRPageText,
    OCRProvider,
    OCRStartRequest,
    OCRStatus,
    ocr_client_request_token,
)
from .processing import (
    ClassificationResult,
    DocumentClassifier,
    DocumentFieldExtractor,
    EvidenceValidationError,
    ExtractionResult,
    IDPOutputError,
    PaidStage,
    PaidStageRecord,
    PaidStageState,
    StageCallLedger,
    parse_classifier_output,
    parse_extractor_output,
    parse_strict_json,
    validate_evidence_anchor,
)
from .rules import DerivedMetadataEngine, DerivedValue, add_calendar_months, add_calendar_years, derive_anniversary

__all__ = [name for name in globals() if not name.startswith("_")]
