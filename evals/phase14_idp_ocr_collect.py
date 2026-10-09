"""Opt-in, operator-run Textract collector for the Phase 14 OCR fixtures.

The default command performs only local manifest/PDF preflight.  The paid path
requires both ``--execute`` and ``--confirm-real-ocr`` and uses one sequential
operator process: eight allowlisted synthetic PDFs, at most four Get polls per
job, and no SNS/Lambda callback.  Raw Textract output is written only beneath
the caller's private temporary artifact directory; stdout contains metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
from legaldesk.idp.acquisition import acquire_pdf  # noqa: E402

from phase14_idp_real_runner import (  # noqa: E402
    MANIFEST_PATH,
    PDF_ROOT,
    RealEvaluationError,
    _canonical_bytes,
    _digest,
    _fixture_bytes,
    _read_json,
    load_manifest,
)

MAX_JOBS = 8
MAX_PAGES = 22
MAX_GET_CALLS = 32
MAX_POLLS_PER_JOB = 4
POLL_SECONDS = 15
MAX_WALL_SECONDS = 60 * 60
SAFE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{2,62}$")
SAFE_ATTEMPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    manifest: Path = MANIFEST_PATH
    pdf_root: Path = PDF_ROOT
    region: str = "eu-west-1"
    source_bucket: str = ""
    allowed_source_buckets: tuple[str, ...] = ()
    output_dir: Path | None = None
    execute: bool = False
    confirm_real_ocr: bool = False
    fixture_ids: tuple[str, ...] = ()
    attempt: str | None = None
    poll_seconds: int = POLL_SECONDS
    wall_seconds: int = MAX_WALL_SECONDS


class OCRCollectionError(RealEvaluationError):
    pass


def _scanned(config: CollectorConfig) -> tuple[Mapping[str, Any], ...]:
    fixtures = load_manifest(config.manifest)
    selected = tuple(item for item in fixtures.values() if "scanned" in item["content_modes"])
    if config.fixture_ids:
        wanted = set(config.fixture_ids)
        if len(wanted) != len(config.fixture_ids) or not wanted.issubset(fixtures):
            raise OCRCollectionError("fixture_selection_invalid")
        selected = tuple(item for item in selected if item["id"] in wanted)
    if len(selected) > MAX_JOBS:
        raise OCRCollectionError("ocr_job_cap_exceeded")
    pages = 0
    for fixture in selected:
        _body, document = _fixture_bytes(fixture, config.pdf_root)
        pages += document.page_count  # Textract bills the entire PDF, not only OCR pages.
    if pages > MAX_PAGES:
        raise OCRCollectionError("ocr_page_cap_exceeded")
    return selected


def preflight(config: CollectorConfig) -> dict[str, object]:
    selected = _scanned(config)
    return {
        "collector": "phase14-idp-ocr-collector-1.0.0",
        "status": "PREFLIGHT",
        "region": config.region,
        "fixtureCount": len(selected),
        "fixtureIds": [str(item["id"]) for item in selected],
        "pdfPages": sum(int(item["page_count"]) for item in selected),
        "limits": {"jobs": MAX_JOBS, "pdfPages": MAX_PAGES, "getCalls": MAX_GET_CALLS, "pollsPerJob": MAX_POLLS_PER_JOB, "wallSeconds": config.wall_seconds},
        "paidCalls": 0,
    }


def _safe_attempt(value: str | None) -> str:
    attempt = value or uuid4().hex
    if SAFE_ATTEMPT.fullmatch(attempt) is None:
        raise OCRCollectionError("attempt_invalid")
    return attempt


def _allowed_buckets(config: CollectorConfig) -> frozenset[str]:
    configured = config.allowed_source_buckets or tuple(
        item.strip() for item in os.environ.get("LEGALDESK_EVAL_SOURCE_BUCKET_ALLOWLIST", "").split(",") if item.strip()
    )
    return frozenset(item for item in configured if SAFE_BUCKET.fullmatch(item))


def _validate_output_dir(path: Path) -> Path:
    root = (ROOT / "tmp").resolve()
    raw = Path(path)
    current_raw = raw
    while current_raw != current_raw.parent:
        if current_raw.is_symlink():
            raise OCRCollectionError("artifact_dir_symlink_forbidden")
        current_raw = current_raw.parent
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise OCRCollectionError("artifact_dir_must_be_under_tmp") from exc
    current = resolved
    while current != root:
        if current.is_symlink():
            raise OCRCollectionError("artifact_dir_symlink_forbidden")
        current = current.parent
    return resolved


def _journal_update(path: Path, state: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise OCRCollectionError("journal_temp_collision")
    temporary.write_bytes(_canonical_bytes(state))
    # OneDrive can briefly hold the destination while indexing.  Retry only
    # this local metadata operation; never retry S3/Textract calls or delete
    # an ambiguous paid-stage object.  On exhaustion the .tmp and .started
    # files remain for operator recovery.
    delays = (0.05, 0.1, 0.2)
    for attempt, delay in enumerate(delays, start=1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == len(delays):
                raise
            time.sleep(delay)


def _artifact_payload(fixture: Mapping[str, Any], *, document: Any, job_id: str, api_calls: int, pages: Mapping[int, str]) -> dict[str, object]:
    canonical_pages = {str(page): pages[page] for page in sorted(pages)}
    return {
        "fixtureId": fixture["id"], "documentSha256": fixture["sha256"], "pageCount": document.page_count,
        "pages": canonical_pages, "pagesSha256": _digest(canonical_pages), "immutable": True,
        "authorization": "operator-approved-preexisting-textract-artifact",
        "stageProof": {"provider": "textract", "status": "SUCCEEDED", "jobId": job_id, "apiCalls": api_calls, "pages": document.page_count},
    }


def _lines(response: Mapping[str, Any]) -> dict[int, str]:
    grouped: dict[int, list[str]] = {}
    for block in response.get("Blocks", []) if isinstance(response.get("Blocks", []), list) else []:
        if not isinstance(block, Mapping) or block.get("BlockType") != "LINE" or not isinstance(block.get("Text"), str):
            continue
        page = block.get("Page", 1)
        if type(page) is int and page >= 1:
            grouped.setdefault(page, []).append(block["Text"])
    return {page: " ".join(lines).strip() for page, lines in grouped.items() if " ".join(lines).strip()}


def _client_config() -> Any:
    try:
        from botocore.config import Config
        # Botocore's ``max_attempts`` excludes the initial request: exactly
        # one SDK retry, while application-level Start/Get operations are
        # never retried or resumed after an ambiguous outcome.
        return Config(retries={"mode": "standard", "total_max_attempts": 1}, connect_timeout=10, read_timeout=90)
    except ImportError as exc:
        raise OCRCollectionError("aws_sdk_unavailable") from exc


def _journal(path: Path, attempt: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    journal = path / f"{attempt}.started"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(journal), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(_canonical_bytes({"collector": "phase14-idp-ocr-collector-1.0.0", "attempt": attempt}))
    finally:
        if fd != -1:
            os.close(fd)
    return journal


def collect(
    config: CollectorConfig,
    *,
    s3_client: Any | None = None,
    textract_client: Any | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    selected = _scanned(config)
    if not config.execute:
        return preflight(config)
    if not config.confirm_real_ocr:
        raise OCRCollectionError("real_ocr_requires_explicit_confirmation")
    if config.region != "eu-west-1":
        raise OCRCollectionError("region_restricted")
    if not config.source_bucket or SAFE_BUCKET.fullmatch(config.source_bucket) is None:
        raise OCRCollectionError("source_bucket_invalid")
    if config.source_bucket not in _allowed_buckets(config):
        raise OCRCollectionError("source_bucket_not_inventoried_allowlist")
    if config.output_dir is None:
        raise OCRCollectionError("artifact_dir_required")
    output_dir = _validate_output_dir(config.output_dir)
    if config.poll_seconds < 1 or config.wall_seconds < 1 or config.wall_seconds > MAX_WALL_SECONDS:
        raise OCRCollectionError("collector_budget_invalid")
    if s3_client is None or textract_client is None:
        try:
            import boto3
            sdk = _client_config()
            s3_client = boto3.client("s3", region_name=config.region, config=sdk)
            textract_client = boto3.client("textract", region_name=config.region, config=sdk)
        except ImportError as exc:
            raise OCRCollectionError("aws_sdk_unavailable") from exc
    attempt = _safe_attempt(config.attempt)
    # Refuse every output collision before creating clients or uploading any
    # source object; a previous attempt must never be mistaken for a fresh one.
    collisions = [output_dir / f"{fixture['id']}.json" for fixture in selected] + [output_dir / "manifest.json", output_dir / f"{attempt}.started"]
    if any(path.exists() or path.is_symlink() for path in collisions):
        raise OCRCollectionError("artifact_exists_before_paid_stage")
    journal = _journal(output_dir, attempt)  # exclusive journal before any upload/paid stage.
    journal_state: dict[str, object] = {"collector": "phase14-idp-ocr-collector-1.0.0", "attempt": attempt, "jobs": []}
    prefix = f"idp-evaluation/ocr/{attempt}/"
    started = now_fn()
    uploaded: list[tuple[str, str, str, int]] = []
    reports: list[dict[str, object]] = []
    get_calls = 0
    try:
        for fixture in selected:
            if now_fn() + 90 >= started + config.wall_seconds:
                raise OCRCollectionError("wall_deadline_exceeded_before_ocr_job")
            body, document = _fixture_bytes(fixture, config.pdf_root)
            key = f"{prefix}{fixture['id']}.pdf"
            digest = hashlib.sha256(body).hexdigest()
            # If this fails, the Start call is intentionally not retried: its outcome is ambiguous.
            s3_client.put_object(Bucket=config.source_bucket, Key=key, Body=body, Metadata={"legaldesk-sha256": digest}, ServerSideEncryption="AES256", IfNoneMatch="*")
            uploaded.append((config.source_bucket, key, digest, len(body)))
            jobs = list(journal_state["jobs"])
            jobs.append({"fixtureId": fixture["id"], "objectKey": key, "status": "UPLOADED"})
            journal_state["jobs"] = jobs
            _journal_update(journal, journal_state)
            token = hashlib.sha256(f"{attempt}:{fixture['id']}:{digest}".encode()).hexdigest()[:64]
            response = textract_client.start_document_text_detection(
                DocumentLocation={"S3Object": {"Bucket": config.source_bucket, "Name": key}},
                ClientRequestToken=token, JobTag=f"legaldesk-idp-eval-{attempt}",
            )
            job_id = response.get("JobId") if isinstance(response, Mapping) else None
            if not isinstance(job_id, str) or not job_id:
                raise OCRCollectionError("textract_job_id_missing")
            jobs[-1]["jobId"] = job_id
            jobs[-1]["status"] = "STARTED"
            _journal_update(journal, journal_state)
            pages: dict[int, str] = {}
            next_token: str | None = None
            terminal: Mapping[str, Any] | None = None
            calls_for_job = 0
            for poll in range(MAX_POLLS_PER_JOB):
                if get_calls >= MAX_GET_CALLS:
                    raise OCRCollectionError("ocr_get_call_cap_exceeded")
                if now_fn() + 90 >= started + config.wall_seconds:
                    raise OCRCollectionError("wall_deadline_exceeded_before_ocr_get")
                request: dict[str, Any] = {"JobId": job_id}
                if next_token is not None:
                    request["NextToken"] = next_token
                answer = textract_client.get_document_text_detection(**request)
                get_calls += 1
                calls_for_job += 1
                if not isinstance(answer, Mapping):
                    raise OCRCollectionError("textract_response_invalid")
                for page, text in _lines(answer).items():
                    pages[page] = f"{pages[page]} {text}".strip() if page in pages else text
                terminal = answer
                status = answer.get("JobStatus")
                if status in {"FAILED", "PARTIAL_SUCCESS"}:
                    break
                # Textract may return a terminal SUCCEEDED page with a
                # continuation token; consume those pages within the same
                # bounded operator poll budget.
                if status == "SUCCEEDED":
                    fresh = answer.get("NextToken")
                    if fresh is not None:
                        raise OCRCollectionError("textract_succeeded_pagination_incomplete")
                    break
                if status != "IN_PROGRESS":
                    raise OCRCollectionError("textract_status_invalid")
                fresh = answer.get("NextToken")
                if fresh is not None and (not isinstance(fresh, str) or not fresh or fresh == next_token):
                    raise OCRCollectionError("textract_pagination_invalid")
                next_token = fresh
                if poll + 1 < MAX_POLLS_PER_JOB:
                    sleep_fn(config.poll_seconds)
            if terminal is None or terminal.get("JobStatus") != "SUCCEEDED":
                raise OCRCollectionError("textract_job_not_succeeded")
            reported_pages = terminal.get("DocumentMetadata", {}).get("Pages") if isinstance(terminal.get("DocumentMetadata"), Mapping) else None
            if type(reported_pages) is not int or reported_pages != document.page_count:
                raise OCRCollectionError("textract_page_count_mismatch")
            required = set(document.ocr_required_pages)
            if not required.issubset(pages) or any(not pages[p].strip() for p in required):
                raise OCRCollectionError("textract_required_pages_missing")
            artifact = _artifact_payload(fixture, document=document, job_id=job_id, api_calls=calls_for_job, pages={p: pages[p] for p in sorted(required)})
            artifact_path = output_dir / f"{fixture['id']}.json"
            if artifact_path.exists():
                raise OCRCollectionError("artifact_exists")
            artifact_path.write_bytes(_canonical_bytes(artifact))
            jobs[-1]["status"] = "SUCCEEDED"
            _journal_update(journal, journal_state)
            reports.append({"fixtureId": fixture["id"], "jobId": job_id, "pageCount": document.page_count, "coveredPages": len(required), "getCalls": calls_for_job, "artifactSha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest()})
        # Delete only our exact objects, and only after all jobs reached a terminal success.
        for bucket, key, digest, size in uploaded:
            head = s3_client.head_object(Bucket=bucket, Key=key)
            metadata = head.get("Metadata", {}) if isinstance(head, Mapping) else {}
            if metadata.get("legaldesk-sha256") != digest or head.get("ContentLength") != size:
                raise OCRCollectionError("cleanup_source_identity_mismatch")
            s3_client.delete_object(Bucket=bucket, Key=key)
        manifest = {str(item["fixtureId"]): str(item["artifactSha256"]) for item in reports}
        manifest_path = output_dir / "manifest.json"
        manifest_path.write_bytes(_canonical_bytes(manifest))
        journal.unlink()
        return {"collector": "phase14-idp-ocr-collector-1.0.0", "status": "COMPLETE", "attempt": attempt, "fixtureCount": len(reports), "pdfPages": sum(int(item["pageCount"]) for item in reports), "getCalls": get_calls, "artifacts": reports, "artifactManifest": str(manifest_path)}
    except Exception:
        # Do not resume or delete objects after an ambiguous/partial paid stage.
        raise


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pdf-root", type=Path, default=PDF_ROOT)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--source-bucket", default="")
    parser.add_argument("--allowed-source-bucket", action="append", dest="allowed_source_buckets", default=[])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--fixture-id", action="append", dest="fixture_ids", default=[])
    parser.add_argument("--attempt")
    parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)
    parser.add_argument("--wall-seconds", type=int, default=MAX_WALL_SECONDS)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-real-ocr", action="store_true")
    args = parser.parse_args(argv)
    config = CollectorConfig(manifest=args.manifest, pdf_root=args.pdf_root, region=args.region, source_bucket=args.source_bucket, allowed_source_buckets=tuple(args.allowed_source_buckets), output_dir=args.output_dir, execute=args.execute, confirm_real_ocr=args.confirm_real_ocr, fixture_ids=tuple(args.fixture_ids), attempt=args.attempt, poll_seconds=args.poll_seconds, wall_seconds=args.wall_seconds)
    try:
        report = collect(config)
    except Exception as exc:
        code = str(exc) if isinstance(exc, OCRCollectionError) else "COLLECTOR_FAILED"
        print(json.dumps({"collector": "phase14-idp-ocr-collector-1.0.0", "status": "FAILED", "code": code}, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
