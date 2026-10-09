"""Opt-in, bounded Phase 14 IDP model evaluation.

The default command is a local preflight.  ``--execute --confirm-real-bedrock``
is the only path that constructs a Bedrock client.  It evaluates the same
18 synthetic PDFs used by the offline oracle, sequentially, through the real
IDP acquisition/pipeline/provider adapters.  Reports are metadata-only:
values and quotes are represented by SHA-256 digests, never copied into the
export.

Scanned pages are intentionally not reconstructed from the fixture manifest.
They require an explicitly authorized, immutable artifact export from an
actual Textract stage (or the case is left unexecuted).  This runner does not
poll Textract and does not invent OCR evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.idp.acquisition import acquire_pdf  # noqa: E402
from legaldesk.idp.artifacts import InMemoryIDPArtifactStore  # noqa: E402
from legaldesk.idp.pipeline import IDPProcessingPipeline  # noqa: E402
from legaldesk.idp.processing import IDPOutputError, StageCallLedger  # noqa: E402
from legaldesk.idp.providers import (  # noqa: E402
    IDPConverseClassifier,
    IDPConverseExtractor,
    IDPModelConfig,
    boto3_idp_runtime_client,
    idp_prompt_identity,
)
from legaldesk.idp.registry import IDPSchemaRegistry  # noqa: E402


MANIFEST_PATH = ROOT / "tests" / "fixtures" / "idp" / "manifest.json"
PDF_ROOT = (ROOT / "tests" / "fixtures" / "idp" / "pdfs").resolve()
RESULTS_ROOT = (ROOT / "evals" / "results").resolve()
MAX_FIXTURES = 18
MAX_BEDROCK_CALLS = 36
MAX_INPUT_TOKENS = 200_000
MAX_OUTPUT_TOKENS = 46_080
MAX_OCR_PAGES = 22
MAX_OCR_API_CALLS = 32
MAX_WALL_SECONDS = 60 * 60
PROVIDER_RESERVE_SECONDS = 120  # 90s SDK read timeout plus persistence margin.
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
APPROVED_MODEL_IDS = frozenset({"eu.anthropic.claude-sonnet-4-6"})


class RealEvaluationError(ValueError):
    """Closed, operator-safe runner failure."""


@dataclass(frozen=True, slots=True)
class EvaluationLimits:
    fixtures: int = MAX_FIXTURES
    bedrock_calls: int = MAX_BEDROCK_CALLS
    input_tokens: int = MAX_INPUT_TOKENS
    output_tokens: int = MAX_OUTPUT_TOKENS
    ocr_pages: int = MAX_OCR_PAGES
    ocr_api_calls: int = MAX_OCR_API_CALLS
    wall_seconds: int = MAX_WALL_SECONDS
    provider_reserve_seconds: int = PROVIDER_RESERVE_SECONDS
    retries: int = 0


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    manifest: Path = MANIFEST_PATH
    pdf_root: Path = PDF_ROOT
    region: str = "eu-west-1"
    model_id: str = "eu.anthropic.claude-sonnet-4-6"
    execute: bool = False
    confirm_real_bedrock: bool = False
    fixture_ids: tuple[str, ...] = ()
    ocr_artifact_dir: Path | None = None
    ocr_artifact_manifest: Path | None = None
    output: Path | None = None
    wall_seconds: int = MAX_WALL_SECONDS
    release_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class OutputReservation:
    output_path: Path
    journal_path: Path
    token: str


def _sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _safe_error(exc: BaseException) -> str:
    name = type(exc).__name__
    return {
        "IDPOutputError": "MODEL_OUTPUT_INVALID",
        "IDPContractError": "IDP_CONTRACT_ERROR",
        "ParamValidationError": "PROVIDER_REQUEST_INVALID",
        "ValidationException": "PROVIDER_VALIDATION",
        "ReadTimeoutError": "PROVIDER_TIMEOUT",
        "EndpointConnectionError": "PROVIDER_CONNECTION_ERROR",
        "NoCredentialsError": "PROVIDER_AUTHENTICATION",
        "PartialCredentialsError": "PROVIDER_AUTHENTICATION",
        "TimeoutError": "PROVIDER_TIMEOUT",
        "ClientError": "PROVIDER_ERROR",
        "RealEvaluationError": "RUNNER_CONTRACT_ERROR",
    }.get(name, "EVALUATION_FAILED")


def _safe_error_type(exc: BaseException) -> str:
    return {
        "IDPOutputError": "IDP_OUTPUT",
        "IDPContractError": "IDP_CONTRACT",
        "ParamValidationError": "SDK_PARAMETER_VALIDATION",
        "ValidationException": "PROVIDER_VALIDATION",
        "ReadTimeoutError": "TIMEOUT",
        "EndpointConnectionError": "CONNECTION",
        "NoCredentialsError": "AUTHENTICATION",
        "PartialCredentialsError": "AUTHENTICATION",
        "TimeoutError": "TIMEOUT",
        "ClientError": "PROVIDER_CLIENT",
        "RealEvaluationError": "RUNNER_CONTRACT",
    }.get(type(exc).__name__, "UNEXPECTED")


def _validate_model_id(value: object) -> str:
    if not isinstance(value, str) or value not in APPROVED_MODEL_IDS:
        raise RealEvaluationError("model_profile_not_allowlisted")
    return value


def _output_paths(path: Path) -> tuple[Path, Path]:
    resolved = path.resolve()
    try:
        resolved.relative_to(RESULTS_ROOT)
    except ValueError as exc:
        raise RealEvaluationError("output_path_out_of_scope") from exc
    if resolved.suffix != ".json" or path.is_symlink():
        raise RealEvaluationError("output_path_invalid")
    return resolved, resolved.with_suffix(resolved.suffix + ".started")


def reserve_output(path: Path) -> OutputReservation:
    """Create an exclusive, metadata-only started journal before paid work."""

    resolved, journal = _output_paths(path)
    if resolved.exists() or journal.exists():
        raise RealEvaluationError("output_exists_or_attempt_already_started")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(journal), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(_canonical_bytes({"runner": "phase14-idp-real-runner-1.0.0", "attempt": token, "output": resolved.name}))
    finally:
        if fd != -1:
            os.close(fd)
    return OutputReservation(resolved, journal, token)


def _read_json(path: Path, label: str, *, max_bytes: int = 2 * 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise RealEvaluationError(f"{label}_unavailable")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RealEvaluationError(f"{label}_invalid") from exc


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict[str, Any]]:
    """Load only server-owned fixture metadata; never use source_pages."""

    payload = _read_json(path, "manifest")
    raw = payload.get("fixtures") if isinstance(payload, Mapping) else None
    if not isinstance(raw, list) or len(raw) != MAX_FIXTURES:
        raise RealEvaluationError("manifest_fixture_count_invalid")
    fixtures: dict[str, dict[str, Any]] = {}
    type_counts: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise RealEvaluationError("manifest_fixture_invalid")
        fixture_id, filename, digest = item.get("id"), item.get("filename"), item.get("sha256")
        page_count, expected_type = item.get("page_count"), item.get("expected_type")
        modes = item.get("content_modes")
        if (
            not isinstance(fixture_id, str) or _SAFE_ID.fullmatch(fixture_id) is None
            or fixture_id in fixtures or not isinstance(filename, str)
            or Path(filename).name != filename or not filename.lower().endswith(".pdf")
            or not _sha(digest) or type(page_count) is not int or not 1 <= page_count <= MAX_OCR_PAGES
            or expected_type not in {"CONTRACT", "DEMAND", "JUDGMENT", "UNKNOWN"}
            or not isinstance(modes, list) or not modes or any(mode not in {"digital", "scanned"} for mode in modes)
        ):
            raise RealEvaluationError("manifest_fixture_invalid")
        fixtures[fixture_id] = {
            "id": fixture_id, "filename": filename, "sha256": digest.lower(),
            "page_count": page_count, "expected_type": expected_type,
            "content_modes": tuple(str(mode) for mode in modes),
        }
        type_counts[expected_type] = type_counts.get(expected_type, 0) + 1
    if type_counts != {"CONTRACT": 5, "DEMAND": 5, "JUDGMENT": 5, "UNKNOWN": 3}:
        raise RealEvaluationError("manifest_type_diversity_invalid")
    return fixtures


def _select(fixtures: Mapping[str, Mapping[str, Any]], ids: tuple[str, ...]) -> tuple[Mapping[str, Any], ...]:
    if not ids:
        return tuple(fixtures.values())
    if len(ids) > MAX_FIXTURES or len(set(ids)) != len(ids) or any(item not in fixtures for item in ids):
        raise RealEvaluationError("fixture_selection_invalid")
    return tuple(fixtures[item] for item in ids)


def _fixture_bytes(fixture: Mapping[str, Any], pdf_root: Path) -> tuple[bytes, Any]:
    path = (pdf_root / str(fixture["filename"])).resolve()
    try:
        path.relative_to(pdf_root.resolve())
    except ValueError as exc:
        raise RealEvaluationError("fixture_path_out_of_scope") from exc
    if path.is_symlink() or not path.is_file():
        raise RealEvaluationError("fixture_pdf_unavailable")
    body = path.read_bytes()
    if len(body) > MAX_PDF_BYTES or hashlib.sha256(body).hexdigest() != fixture["sha256"]:
        raise RealEvaluationError("fixture_hash_mismatch")
    document = acquire_pdf(body, expected_sha256=fixture["sha256"], expected_page_count=int(fixture["page_count"]))
    return body, document


def _load_ocr_artifact(
    fixture: Mapping[str, Any], *, document: Any, artifact_dir: Path, artifact_manifest: Mapping[str, Any], limits: EvaluationLimits,
) -> tuple[dict[int, str], dict[str, object]]:
    """Validate a preexisting, operator-authorized Textract page artifact."""

    fixture_id = str(fixture["id"])
    path = (artifact_dir / f"{fixture_id}.json").resolve()
    try:
        path.relative_to(artifact_dir.resolve())
    except ValueError as exc:
        raise RealEvaluationError("ocr_artifact_path_out_of_scope") from exc
    raw_bytes = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
    expected_file_sha = artifact_manifest.get(fixture_id)
    if not _sha(expected_file_sha) or hashlib.sha256(raw_bytes).hexdigest() != str(expected_file_sha).lower() or len(raw_bytes) > MAX_ARTIFACT_BYTES:
        raise RealEvaluationError("ocr_artifact_hash_invalid")
    artifact = json.loads(raw_bytes.decode("utf-8"))
    required = {"fixtureId", "documentSha256", "pageCount", "pages", "immutable", "authorization", "stageProof", "pagesSha256"}
    if not isinstance(artifact, Mapping) or not required.issubset(artifact) or artifact.get("fixtureId") != fixture_id:
        raise RealEvaluationError("ocr_artifact_invalid")
    if artifact.get("documentSha256") != fixture["sha256"] or artifact.get("pageCount") != document.page_count or artifact.get("immutable") is not True or artifact.get("authorization") != "operator-approved-preexisting-textract-artifact":
        raise RealEvaluationError("ocr_artifact_identity_invalid")
    proof = artifact.get("stageProof")
    if not isinstance(proof, Mapping) or proof.get("provider") != "textract" or proof.get("status") != "SUCCEEDED" or not isinstance(proof.get("jobId"), str) or not proof["jobId"].strip():
        raise RealEvaluationError("ocr_stage_proof_invalid")
    api_calls, pages_observed = proof.get("apiCalls"), proof.get("pages")
    if type(api_calls) is not int or type(pages_observed) is not int or api_calls < 1 or pages_observed != document.page_count or api_calls > limits.ocr_api_calls:
        raise RealEvaluationError("ocr_stage_budget_invalid")
    pages_raw = artifact.get("pages")
    if not isinstance(pages_raw, Mapping):
        raise RealEvaluationError("ocr_pages_invalid")
    page_map: dict[int, str] = {}
    for raw_page, text in pages_raw.items():
        if not isinstance(raw_page, str) or not raw_page.isdigit() or type(text) is not str or not text.strip():
            raise RealEvaluationError("ocr_page_invalid")
        page = int(raw_page)
        if page < 1 or page > document.page_count:
            raise RealEvaluationError("ocr_page_out_of_range")
        page_map[page] = text
    required_pages = set(document.ocr_required_pages)
    if set(page_map) != required_pages:
        raise RealEvaluationError("ocr_page_coverage_incomplete")
    canonical_pages = {str(page): page_map[page] for page in sorted(page_map)}
    if artifact.get("pagesSha256") != _digest(canonical_pages):
        raise RealEvaluationError("ocr_pages_hash_invalid")
    if len(required_pages) > limits.ocr_pages:
        raise RealEvaluationError("ocr_page_budget_exceeded")
    return page_map, {"mode": "preexisting_textract_artifact", "artifactSha256": str(expected_file_sha).lower(), "apiCalls": api_calls, "pages": pages_observed, "coveredPages": len(required_pages)}


class _Budget:
    def __init__(self, limits: EvaluationLimits, deadline: float) -> None:
        self.limits, self.deadline = limits, deadline
        self.calls = self.input_tokens = self.output_tokens = 0
        self.ocr_pages = self.ocr_api_calls = 0
        self._reserved_input = self._reserved_output = 0
        self.halted = False
        self.usage_unknown = False
        self.current_stage: str | None = None
        self.failure_stage: str | None = None

    def reserve_ocr(self, pages: int, api_calls: int) -> None:
        """Account for an already completed, externally proven OCR stage."""

        if type(pages) is not int or type(api_calls) is not int or pages < 0 or api_calls < 0:
            raise RealEvaluationError("ocr_usage_metadata_invalid")
        if self.ocr_pages + pages > self.limits.ocr_pages or self.ocr_api_calls + api_calls > self.limits.ocr_api_calls:
            raise RealEvaluationError("ocr_budget_exceeded_before_next_stage")
        self.ocr_pages += pages
        self.ocr_api_calls += api_calls

    def before_converse(self, request: Mapping[str, Any]) -> tuple[int, int]:
        output_config = request.get("outputConfig") if isinstance(request, Mapping) else None
        structure = output_config.get("textFormat", {}).get("structure", {}) if isinstance(output_config, Mapping) else {}
        schema = structure.get("jsonSchema", {}) if isinstance(structure, Mapping) else {}
        schema_name = schema.get("name") if isinstance(schema, Mapping) else None
        self.current_stage = "CLASSIFIER" if schema_name == "legaldesk_idp_classifier" else "EXTRACTOR" if schema_name == "legaldesk_idp_extractor" else "UNKNOWN"
        if self.halted:
            raise RealEvaluationError("paid_call_budget_halted")
        if time.monotonic() + self.limits.provider_reserve_seconds >= self.deadline:
            self.halted = True
            raise RealEvaluationError("wall_deadline_exceeded_before_bedrock_call")
        if self.calls >= self.limits.bedrock_calls:
            self.halted = True
            raise RealEvaluationError("bedrock_call_budget_exceeded_before_call")
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        # Tokenizers vary by model and language.  A bytes/4 heuristic can
        # under-reserve Spanish and JSON punctuation, so the pre-call hard
        # guard uses one token per UTF-8 byte plus a framing allowance.  The
        # provider's actual usage remains authoritative in the report.
        input_reserve = max(1, len(encoded) + 256)
        inference = request.get("inferenceConfig") if isinstance(request, Mapping) else {}
        output_reserve = inference.get("maxTokens") if isinstance(inference, Mapping) else None
        if type(output_reserve) is not int or output_reserve < 1:
            raise RealEvaluationError("provider_output_bound_missing_before_call")
        if self.input_tokens + self._reserved_input + input_reserve > self.limits.input_tokens:
            self.halted = True
            raise RealEvaluationError("input_token_budget_exceeded_before_call")
        if self.output_tokens + self._reserved_output + output_reserve > self.limits.output_tokens:
            self.halted = True
            raise RealEvaluationError("output_token_budget_exceeded_before_call")
        self.calls += 1
        self._reserved_input += input_reserve
        self._reserved_output += output_reserve
        return input_reserve, output_reserve

    def after_converse(self, reservation: tuple[int, int], response: Mapping[str, Any]) -> None:
        input_reserve, output_reserve = reservation
        usage = response.get("usage") if isinstance(response, Mapping) else None
        if not isinstance(usage, Mapping) or type(usage.get("inputTokens")) is not int or type(usage.get("outputTokens")) is not int:
            raise RealEvaluationError("provider_usage_metadata_missing")
        input_tokens, output_tokens = usage["inputTokens"], usage["outputTokens"]
        if input_tokens < 0 or output_tokens < 0:
            raise RealEvaluationError("provider_usage_metadata_invalid")
        self._reserved_input -= input_reserve
        self._reserved_output -= output_reserve
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if self.input_tokens > self.limits.input_tokens or self.output_tokens > self.limits.output_tokens:
            self.halted = True
            raise RealEvaluationError("provider_usage_budget_exceeded")

    def mark_paid_outcome_unknown(self) -> None:
        # Keep reservations and stop the entire run.  A missing usage record
        # cannot be represented as zero and must never permit another paid
        # call or a falsely complete report.
        self.halted = True
        self.usage_unknown = True
        self.failure_stage = self.current_stage


class _BoundedConverse:
    def __init__(self, client: Any, budget: _Budget) -> None:
        self.client, self.budget = client, budget

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        reservation = self.budget.before_converse(kwargs)
        try:
            response = self.client.converse(**kwargs)
        except BaseException:
            self.budget.mark_paid_outcome_unknown()
            raise
        if not isinstance(response, Mapping):
            self.budget.mark_paid_outcome_unknown()
            raise RealEvaluationError("provider_response_invalid")
        try:
            self.budget.after_converse(reservation, response)
        except RealEvaluationError:
            if "usage" not in response or not isinstance(response.get("usage"), Mapping) or type(response.get("usage", {}).get("inputTokens")) is not int or type(response.get("usage", {}).get("outputTokens")) is not int:
                self.budget.mark_paid_outcome_unknown()
            else:
                self.budget.halted = True
            raise
        return response


def _field_export(field: Any) -> dict[str, object]:
    return {
        "presence": field.presence.value,
        "origin": field.origin.value,
        "acceptance": field.acceptance.value,
        "valueDigest": _digest(field.value) if field.presence.value == "PRESENT" else None,
        "evidence": [
            {"page": anchor.page, "quoteDigest": hashlib.sha256(anchor.quote.encode("utf-8")).hexdigest(), "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end}
            for anchor in field.evidence
        ],
    }


def _empty_case(fixture: Mapping[str, Any], status: str, errors: list[str], *, source_hash: str | None = None, ocr: Mapping[str, object] | None = None) -> dict[str, object]:
    return {
        "fixtureId": fixture["id"], "documentType": fixture["expected_type"], "documentSha256": source_hash or fixture["sha256"],
        "status": status, "fields": {}, "derived": None, "errors": sorted(set(errors)), "ocr": dict(ocr or {}), "observed": {},
    }


def _inspect(config: RunnerConfig, fixtures: Mapping[str, Mapping[str, Any]], selected: tuple[Mapping[str, Any], ...]) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for fixture in selected:
        try:
            _body, document = _fixture_bytes(fixture, config.pdf_root)
            ocr_required = list(document.ocr_required_pages)
            ocr = {"requiredPages": ocr_required}
            if ocr_required and config.ocr_artifact_dir is None:
                cases.append(_empty_case(fixture, "NOT_EXECUTED", ["OCR_ARTIFACT_REQUIRED"], ocr=ocr))
            else:
                cases.append(_empty_case(fixture, "NOT_EXECUTED", [], ocr=ocr))
        except Exception as exc:
            cases.append(_empty_case(fixture, "FAILED", [_safe_error(exc)]))
    return cases


def run(
    config: RunnerConfig,
    *,
    client_factory: Callable[[str], Any] | None = None,
    _output_reservation: OutputReservation | None = None,
) -> dict[str, object]:
    if config.execute and not config.confirm_real_bedrock:
        raise RealEvaluationError("real_bedrock_requires_explicit_confirmation")
    _validate_model_id(config.model_id)
    if config.region != "eu-west-1":
        raise RealEvaluationError("region_restricted")
    if config.wall_seconds < 1 or config.wall_seconds > MAX_WALL_SECONDS:
        raise RealEvaluationError("wall_budget_invalid")
    fixtures = load_manifest(config.manifest)
    selected = _select(fixtures, config.fixture_ids)
    plan = _inspect(config, fixtures, selected)
    started = time.monotonic()
    provenance: dict[str, object] = {
        "kind": "unknown", "region": config.region, "model_id": config.model_id,
        "manifest_sha256": hashlib.sha256(config.manifest.read_bytes()).hexdigest(),
        "runner": "phase14-idp-real-runner-1.0.0", "status": "PREFLIGHT" if not config.execute else "INCOMPLETE",
    }
    if config.release_sha256 is not None:
        if not _sha(config.release_sha256):
            raise RealEvaluationError("release_sha256_invalid")
        provenance["release_sha256"] = config.release_sha256.lower()
    if not config.execute:
        return {"provenance": provenance, "limits": asdict(EvaluationLimits(wall_seconds=config.wall_seconds)), "results": [], "plan": plan}

    if config.output is None:
        raise RealEvaluationError("output_required_before_execution")
    reservation = _output_reservation or reserve_output(config.output)
    if reservation.output_path != config.output.resolve():
        raise RealEvaluationError("output_reservation_mismatch")

    raw_client = client_factory(config.region) if client_factory is not None else boto3_idp_runtime_client(region=config.region)
    budget = _Budget(EvaluationLimits(wall_seconds=config.wall_seconds), started + config.wall_seconds)
    bounded_client = _BoundedConverse(raw_client, budget)
    model_config = IDPModelConfig(config.model_id)
    classifier = IDPConverseClassifier(bounded_client, config=model_config)
    extractor = IDPConverseExtractor(bounded_client, config=model_config)
    results: list[dict[str, object]] = []
    ocr_manifest: Mapping[str, Any] = {}
    if config.ocr_artifact_manifest is not None:
        raw_ocr_manifest = _read_json(config.ocr_artifact_manifest, "ocr_artifact_manifest")
        if not isinstance(raw_ocr_manifest, Mapping):
            raise RealEvaluationError("ocr_artifact_manifest_invalid")
        ocr_manifest = raw_ocr_manifest
    for index, fixture in enumerate(selected):
        if time.monotonic() >= budget.deadline:
            results.append(_empty_case(fixture, "NOT_EXECUTED", ["WALL_DEADLINE_EXCEEDED_BEFORE_FIXTURE"]))
            continue
        try:
            input_before, output_before = budget.input_tokens, budget.output_tokens
            body, document = _fixture_bytes(fixture, config.pdf_root)
            pages = dict(document.page_map())
            ocr_meta: dict[str, object] = {"requiredPages": list(document.ocr_required_pages)}
            if document.ocr_required_pages:
                if config.ocr_artifact_dir is None or config.ocr_artifact_manifest is None:
                    results.append(_empty_case(fixture, "NOT_EXECUTED", ["OCR_ARTIFACT_REQUIRED"], ocr=ocr_meta))
                    continue
                ocr_pages, proof = _load_ocr_artifact(fixture, document=document, artifact_dir=config.ocr_artifact_dir, artifact_manifest=ocr_manifest, limits=budget.limits)
                pages.update(ocr_pages)
                budget.reserve_ocr(int(proof["pages"]), int(proof["apiCalls"]))
                ocr_meta.update(proof)
            if set(pages) != set(range(1, document.page_count + 1)):
                raise RealEvaluationError("page_coverage_incomplete_before_bedrock_call")
            pipeline = IDPProcessingPipeline(
                classifier=classifier, extractor=extractor, registry=IDPSchemaRegistry(),
                artifact_store=InMemoryIDPArtifactStore(), stage_ledger=StageCallLedger(),
            )
            result = pipeline.process_pages(
                tenant_id="evaluation-tenant", matter_id="evaluation-matter", document_id=str(fixture["id"]),
                run_id=f"evaluation-{fixture['id']}", pages=pages, content_sha256=str(fixture["sha256"]),
                model_id=config.model_id, prompt_version=idp_prompt_identity(), deadline_at=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=max(1, int(budget.deadline - time.monotonic()))),
            )
            if result.run is None:
                raise RealEvaluationError("pipeline_run_missing")
            fields = {name: _field_export(field) for name, field in result.run.fields.items()}
            status = "REVIEW_REQUIRED" if any(value["acceptance"] == "REVIEW_REQUIRED" for value in fields.values()) else "COMPLETED"
            results.append({
                "fixtureId": fixture["id"], "documentType": result.run.document_type.value,
                "documentSha256": result.run.document_sha256, "status": status, "fields": fields,
                "derived": None, "ocr": ocr_meta,
                "observed": {"tokens": {"input": budget.input_tokens - input_before, "output": budget.output_tokens - output_before}, "ocr": {"pages": ocr_meta.get("pages", 0), "api_calls": ocr_meta.get("apiCalls", 0)}},
            })
        except Exception as exc:
            errors = [_safe_error(exc)]
            if budget.usage_unknown:
                errors.append("PAID_OUTCOME_UNKNOWN")
            failed = _empty_case(fixture, "FAILED", errors)
            failed["diagnostic"] = {
                "stage": budget.failure_stage or budget.current_stage or "UNKNOWN",
                "code": _safe_error(exc), "type": _safe_error_type(exc),
            }
            results.append(failed)
            if budget.halted:
                for remaining in selected[index + 1:]:
                    results.append(_empty_case(remaining, "NOT_EXECUTED", ["PAID_OUTCOME_UNKNOWN" if budget.usage_unknown else "PAID_BUDGET_STOPPED"]))
                break
    if budget.usage_unknown:
        run_status = "UNKNOWN"
        provenance_kind = "unknown"
    elif not results or any(item.get("status") in {"FAILED", "NOT_EXECUTED"} for item in results):
        run_status = "PARTIAL"
        provenance_kind = "real_model" if budget.calls else "unknown"
    else:
        run_status = "COMPLETE"
        provenance_kind = "real_model" if budget.calls else "unknown"
    provenance.update({"kind": provenance_kind, "status": run_status, "prompt_version": idp_prompt_identity(), "calls": budget.calls, "input_estimate": "UTF-8 request bytes plus 256 framing bytes; actual provider usage is required"})
    report = {
        "provenance": provenance, "limits": asdict(budget.limits),
        "usage": {"bedrockCalls": budget.calls, "inputTokens": None if budget.usage_unknown else budget.input_tokens, "outputTokens": None if budget.usage_unknown else budget.output_tokens, "ocrPages": budget.ocr_pages, "ocrApiCalls": budget.ocr_api_calls, "retries": 0, "usageUnknown": budget.usage_unknown},
        "results": results, "plan": plan,
    }
    write_immutable(config.output, report, reservation=reservation)
    return report


def write_immutable(path: Path, payload: Mapping[str, object], *, reservation: OutputReservation | None = None) -> None:
    resolved, journal = _output_paths(path)
    if reservation is not None:
        if reservation.output_path != resolved or reservation.journal_path != journal or not journal.is_file():
            raise RealEvaluationError("output_reservation_invalid")
    elif resolved.exists() or journal.exists():
        raise RealEvaluationError("output_exists_or_attempt_already_started")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(resolved), flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(_canonical_bytes(payload))
    finally:
        if fd != -1:
            os.close(fd)
    if reservation is not None:
        journal.unlink()


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    parser.add_argument("--pdf-root", type=Path, default=PDF_ROOT)
    parser.add_argument("--region", default="eu-west-1")
    parser.add_argument("--model-id", default="eu.anthropic.claude-sonnet-4-6")
    parser.add_argument("--fixture-id", action="append", dest="fixture_ids", default=[])
    parser.add_argument("--ocr-artifact-dir", type=Path)
    parser.add_argument("--ocr-artifact-manifest", type=Path)
    parser.add_argument("--wall-seconds", type=int, default=MAX_WALL_SECONDS)
    parser.add_argument("--release-sha256")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-real-bedrock", action="store_true")
    args = parser.parse_args(argv)
    config = RunnerConfig(
        manifest=args.manifest, pdf_root=args.pdf_root, region=args.region, model_id=args.model_id,
        execute=args.execute, confirm_real_bedrock=args.confirm_real_bedrock,
        fixture_ids=tuple(args.fixture_ids), ocr_artifact_dir=args.ocr_artifact_dir,
        ocr_artifact_manifest=args.ocr_artifact_manifest, output=args.output,
        wall_seconds=args.wall_seconds, release_sha256=args.release_sha256,
    )
    try:
        report = run(config)
        if not args.execute:
            write_immutable(args.output, report)
    except (RealEvaluationError, OSError, ValueError) as exc:
        print(f"phase14_idp_real_runner: {_safe_error(exc)}", file=sys.stderr)
        return 2
    if args.execute and report["provenance"].get("status") != "COMPLETE":
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
