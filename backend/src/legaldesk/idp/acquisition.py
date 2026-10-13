"""Bounded, page-aware PDF acquisition for IDP.

This module only reads bytes supplied by an already-authorized caller.  It
does not read S3, infer ownership, or invoke OCR.  Empty pages are returned
as an explicit coverage gap so the asynchronous OCR continuation can take
over without pretending that a partial document was complete.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from .models import DEFAULT_MAX_BYTES, DEFAULT_MAX_PAGES, IDPContractError

try:  # Keep import failure explicit for release environments missing pypdf.
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - exercised by packaging, not unit tests
    PdfReader = None  # type: ignore[assignment,misc]


MAX_PAGE_TEXT_CHARS = 250_000
MAX_TOTAL_TEXT_CHARS = 4_000_000
MAX_PAGE_RESOURCES = 256
MAX_TOTAL_RESOURCES = 10_000
MAX_IMAGE_PIXELS = 50_000_000
MAX_COMPRESSED_CONTENT_STREAM_BYTES = 32 * 1024 * 1024
MAX_DECOMPRESSED_STREAM_BYTES = 75 * 1024 * 1024  # documented library ceiling; extraction never disables pypdf's finite guard.
MAX_FORM_XOBJECT_DEPTH = 8


class PDFAcquisitionError(IDPContractError):
    """The bytes are not a usable PDF or cannot be safely extracted."""


class PDFLimitExceeded(PDFAcquisitionError):
    """The document must be IDP_SKIPPED rather than silently truncated."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def normalize_page_text(value: str) -> str:
    """Normalize layout whitespace while retaining source-language text."""

    value = unicodedata.normalize("NFKC", value).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


@dataclass(frozen=True, slots=True)
class IDPPageText:
    page: int
    text: str
    normalized_text: str
    extracted: bool
    has_raster_content: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.page, int) or self.page < 1:
            raise PDFAcquisitionError("page number is invalid")
        if not isinstance(self.text, str) or not isinstance(self.normalized_text, str) or not isinstance(self.has_raster_content, bool):
            raise PDFAcquisitionError("page text is invalid")


@dataclass(frozen=True, slots=True)
class PDFTextDocument:
    content_sha256: str
    page_count: int
    pages: tuple[IDPPageText, ...]
    ocr_required_pages: tuple[int, ...]
    coverage_complete: bool

    def page_map(self) -> Mapping[int, str]:
        return {page.page: page.normalized_text for page in self.pages}


def acquire_pdf(
    content: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    expected_sha256: str | None = None,
    expected_page_count: int | None = None,
) -> PDFTextDocument:
    """Extract all digital page text, or raise an explicit bounded outcome."""

    if not isinstance(content, bytes):
        raise PDFAcquisitionError("PDF content must be bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise PDFAcquisitionError("max_bytes is invalid")
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
        raise PDFAcquisitionError("max_pages is invalid")
    content_sha256 = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and content_sha256 != expected_sha256.lower():
        raise PDFAcquisitionError("PDF content hash does not match the server-owned version")
    if len(content) > max_bytes:
        raise PDFLimitExceeded("SIZE_LIMIT")
    if not content.startswith(b"%PDF-"):
        raise PDFAcquisitionError("document is not a PDF")
    if PdfReader is None:
        raise PDFAcquisitionError("pypdf is unavailable")

    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            try:
                decrypted = reader.decrypt("")
            except Exception as exc:  # pragma: no cover - pypdf version detail
                raise PDFAcquisitionError("encrypted PDF requires a password") from exc
            if not decrypted:
                raise PDFAcquisitionError("encrypted PDF requires a password")
        page_count = len(reader.pages)
    except PDFLimitExceeded:
        raise
    except PDFAcquisitionError:
        raise
    except Exception as exc:
        raise PDFAcquisitionError("PDF cannot be parsed safely") from exc

    if page_count < 1:
        raise PDFAcquisitionError("PDF has no pages")
    if page_count > max_pages:
        raise PDFLimitExceeded("PAGE_LIMIT")
    if expected_page_count is not None and page_count != expected_page_count:
        raise PDFAcquisitionError("PDF page count changed after server verification")

    raster_pages: set[int] = set()
    resource_total = 0
    compressed_stream_total = 0
    seen_xobjects: set[int] = set()

    def inspect_resources(resources: object, *, depth: int) -> bool:
        """Conservatively discover nested Form->Image resources."""

        nonlocal resource_total
        if depth > MAX_FORM_XOBJECT_DEPTH:
            raise PDFLimitExceeded("RESOURCE_DEPTH_LIMIT")
        if hasattr(resources, "get_object"):
            resources = resources.get_object()
        if not hasattr(resources, "get"):
            return False
        xobjects = resources.get("/XObject")
        if xobjects is None:
            return False
        if hasattr(xobjects, "get_object"):
            xobjects = xobjects.get_object()
        if not hasattr(xobjects, "values"):
            raise PDFAcquisitionError("XObject resource dictionary is invalid")
        found_image = False
        for reference in xobjects.values():
            resource = reference.get_object() if hasattr(reference, "get_object") else reference
            marker = id(resource)
            if marker in seen_xobjects:
                continue
            seen_xobjects.add(marker)
            resource_total += 1
            if resource_total > MAX_TOTAL_RESOURCES:
                raise PDFLimitExceeded("RESOURCE_LIMIT")
            if not hasattr(resource, "get"):
                raise PDFAcquisitionError("XObject resource is invalid")
            subtype = resource.get("/Subtype")
            if subtype == "/Image":
                width, height = resource.get("/Width"), resource.get("/Height")
                if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0 and width * height > MAX_IMAGE_PIXELS:
                    raise PDFLimitExceeded("IMAGE_RESOURCE_LIMIT")
                found_image = True
            elif subtype == "/Form":
                found_image = inspect_resources(resource.get("/Resources") or {}, depth=depth + 1) or found_image
        return found_image

    # Inspect resource dictionaries before invoking text extraction.  A page
    # with a digital header over an image body is not considered fully covered.
    for number, page in enumerate(reader.pages, start=1):
        try:
            # An indirect XObject can be reused on another page; it must be
            # deduplicated within one page's recursion, not globally, or the
            # second page could lose its conservative OCR requirement.
            seen_xobjects = set()
            contents = page.get("/Contents")
            content_items = contents if isinstance(contents, list) else [contents] if contents is not None else []
            for reference in content_items:
                stream = reference.get_object() if hasattr(reference, "get_object") else reference
                length = stream.get("/Length") if hasattr(stream, "get") else None
                if isinstance(length, int) and length >= 0:
                    compressed_stream_total += length
                    if compressed_stream_total > MAX_COMPRESSED_CONTENT_STREAM_BYTES:
                        raise PDFLimitExceeded("CONTENT_STREAM_LIMIT")
            resources = page.get("/Resources") or {}
            if hasattr(resources, "get_object"):
                resources = resources.get_object()
            if not hasattr(resources, "get"):
                raise PDFAcquisitionError("page resources are invalid")
            before = resource_total
            found_image = inspect_resources(resources, depth=0)
            if resource_total - before > MAX_PAGE_RESOURCES:
                raise PDFLimitExceeded("RESOURCE_LIMIT")
            if found_image:
                raster_pages.add(number)
        except PDFLimitExceeded:
            raise
        except Exception as exc:
            raise PDFAcquisitionError(f"page {number} resources cannot be inspected safely") from exc

    pages: list[IDPPageText] = []
    ocr_pages: list[int] = []
    total_chars = 0
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise PDFAcquisitionError(f"page {number} text extraction failed") from exc
        if not isinstance(text, str):
            raise PDFAcquisitionError(f"page {number} text extraction returned an invalid value")
        if len(text) > MAX_PAGE_TEXT_CHARS:
            raise PDFLimitExceeded("TEXT_LIMIT")
        normalized = normalize_page_text(text)
        total_chars += len(normalized)
        if total_chars > MAX_TOTAL_TEXT_CHARS:
            raise PDFLimitExceeded("TEXT_LIMIT")
        has_raster = number in raster_pages
        extracted = bool(normalized) and not has_raster
        if not extracted:
            ocr_pages.append(number)
        pages.append(IDPPageText(number, text, normalized, extracted, has_raster))

    return PDFTextDocument(
        content_sha256=content_sha256,
        page_count=page_count,
        pages=tuple(pages),
        ocr_required_pages=tuple(ocr_pages),
        coverage_complete=not ocr_pages,
    )


__all__ = [
    "IDPPageText",
    "MAX_PAGE_TEXT_CHARS",
    "MAX_TOTAL_TEXT_CHARS",
    "MAX_PAGE_RESOURCES",
    "MAX_TOTAL_RESOURCES",
    "MAX_IMAGE_PIXELS",
    "MAX_COMPRESSED_CONTENT_STREAM_BYTES",
    "MAX_DECOMPRESSED_STREAM_BYTES",
    "PDFAcquisitionError",
    "PDFLimitExceeded",
    "PDFTextDocument",
    "acquire_pdf",
    "normalize_page_text",
]
