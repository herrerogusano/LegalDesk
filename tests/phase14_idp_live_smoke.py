"""Bounded production-IDP smoke runner (execution is explicit and opt-in).

The runner uses the existing synthetic Cognito-user lifecycle from the public
smoke, but never accepts a real user's credentials and never obtains the IDP
machine secret.  Its default mode is a no-network preflight.  The live mode
only launches the browser child after validating the synthetic fixture and all
local bounds; provider-side paid-call ceilings remain a deployment/runtime
responsibility unless an authorized metadata projection exposes them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = (ROOT / "tests" / "fixtures" / "idp").resolve()
PDF_ROOT = (FIXTURE_ROOT / "pdfs").resolve()
SMOKE_ROOT = (FIXTURE_ROOT / "smoke").resolve()
BROWSER_RUNNER = ROOT / "tests" / "phase14_idp_live_browser.cjs"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
SMOKE_MANIFEST_PATH = SMOKE_ROOT / "smoke-manifest.json"
REGION = "eu-west-1"
RUNNER_VERSION = "1.0.0"
MAX_MODEL_CALLS = 8
MAX_OCR_PAGES = 22
MAX_OCR_API_CALLS = 16
MAX_POLL_ITERATIONS = 20
MAX_BROWSER_API_CALLS = 120
MAX_WALL_SECONDS = 30 * 60
MAX_OUTPUT_TOKENS = 10_240
MAX_INPUT_TOKENS = 80_000
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
sys.path.insert(0, str(ROOT / "tests"))

from phase14_public_smoke import (  # noqa: E402
    CallBudget,
    derive_memory_scope,
    discover_subject_state,
    query_history_state,
)


class LiveIDPPreflightError(ValueError):
    """Closed, operator-safe preflight failure."""


class _DeadlineBudget:
    """CallBudget wrapper that checks the wall deadline before every call."""

    def __init__(self, budget: CallBudget, deadline: float) -> None:
        self._budget = budget
        self._deadline = deadline

    @property
    def count(self) -> int:
        return self._budget.count

    def call(self, service: str, function: Any, **kwargs: object) -> Any:
        if time.monotonic() >= self._deadline:
            raise LiveIDPPreflightError("wall_deadline_exceeded_before_provider_call")
        return self._budget.call(service, function, **kwargs)


@dataclass(frozen=True, slots=True)
class IDPLiveSmokeLimits:
    model_calls: int = MAX_MODEL_CALLS
    input_tokens: int = MAX_INPUT_TOKENS
    output_tokens: int = MAX_OUTPUT_TOKENS
    ocr_pages: int = MAX_OCR_PAGES
    ocr_api_calls: int = MAX_OCR_API_CALLS
    poll_iterations: int = MAX_POLL_ITERATIONS
    wall_seconds: int = MAX_WALL_SECONDS
    retry_count: int = 0


@dataclass(frozen=True, slots=True)
class IDPLiveSmokeConfig:
    base_url: str
    idp_host: str
    matter_id: str
    cross_matter_id: str
    user_pool_id: str
    table_name: str
    memory_id: str
    fixture_path: Path
    fixture_id: str
    region: str = REGION
    attempt_id: str = "local-preflight"
    report_path: Path | None = None
    execute: bool = False
    expected_source: str = "IDP"
    field_name: str = ""
    review_field_name: str = ""
    review_action: str = "none"
    expected_status: str = ""


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value.strip()) is None:
        raise LiveIDPPreflightError(f"{label}_invalid")
    return value.strip()


def _load_manifest() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveIDPPreflightError("fixture_manifest_unavailable") from exc
    fixtures = payload.get("fixtures") if isinstance(payload, Mapping) else None
    if not isinstance(fixtures, list) or not fixtures or len(fixtures) > 18:
        raise LiveIDPPreflightError("fixture_manifest_invalid")
    result: dict[str, dict[str, Any]] = {}
    for fixture in fixtures:
        if not isinstance(fixture, Mapping):
            raise LiveIDPPreflightError("fixture_manifest_invalid")
        fixture_id = fixture.get("id")
        filename = fixture.get("filename")
        digest = fixture.get("sha256")
        page_count = fixture.get("page_count")
        if (
            not isinstance(fixture_id, str)
            or not _SAFE_ID.fullmatch(fixture_id)
            or not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.lower().endswith(".pdf")
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest.lower())
            or type(page_count) is not int
            or not 1 <= page_count <= MAX_OCR_PAGES
            or fixture_id in result
        ):
            raise LiveIDPPreflightError("fixture_manifest_invalid")
        result[fixture_id] = {
            "id": fixture_id,
            "filename": filename,
            "sha256": digest.lower(),
            "page_count": page_count,
            "expected_type": fixture.get("expected_type"),
        }
    return result


def _load_smoke_manifest() -> dict[str, dict[str, Any]]:
    """Load the separate, tiny non-PDF smoke allowlist.

    The ground-truth PDF manifest remains intentionally unchanged at 18
    entries. This manifest is only for the unsupported-media regression smoke
    and cannot add files to the evaluation corpus.
    """

    try:
        payload = json.loads(SMOKE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveIDPPreflightError("smoke_fixture_manifest_unavailable") from exc
    fixtures = payload.get("fixtures") if isinstance(payload, Mapping) else None
    if not isinstance(fixtures, list) or len(fixtures) != 1:
        raise LiveIDPPreflightError("smoke_fixture_manifest_invalid")
    result: dict[str, dict[str, Any]] = {}
    for fixture in fixtures:
        fixture_id = fixture.get("id") if isinstance(fixture, Mapping) else None
        filename = fixture.get("filename") if isinstance(fixture, Mapping) else None
        digest = fixture.get("sha256") if isinstance(fixture, Mapping) else None
        page_count = fixture.get("page_count") if isinstance(fixture, Mapping) else None
        size_bytes = fixture.get("size_bytes") if isinstance(fixture, Mapping) else None
        if (
            not isinstance(fixture, Mapping)
            or not isinstance(fixture_id, str) or not _SAFE_ID.fullmatch(fixture_id)
            or not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".txt")
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest.lower())
            or type(page_count) is not int or page_count != 0
            or type(size_bytes) is not int or not 1 <= size_bytes <= 20 * 1024 * 1024
            or fixture_id in result
            or fixture.get("expected_status") not in {"IDP_SKIPPED", "IDP_FAILED"}
            or fixture.get("expected_source") not in {"RAG", "NONE"}
            or not isinstance(fixture.get("field_name"), str) or not _SAFE_ID.fullmatch(fixture["field_name"])
            or not isinstance(fixture.get("question"), str) or not fixture["question"].strip()
            or fixture.get("skip_reason") != "UNSUPPORTED_MEDIA_TYPE"
        ):
            raise LiveIDPPreflightError("smoke_fixture_manifest_invalid")
        result[fixture_id] = {
            "id": fixture_id,
            "filename": filename,
            "sha256": digest.lower(),
            "page_count": page_count,
            "size_bytes": size_bytes,
            "expected_type": fixture.get("expected_type"),
            "expected_status": fixture["expected_status"],
            "expected_source": fixture["expected_source"],
            "field_name": fixture["field_name"],
            "question": fixture["question"],
            "skip_reason": fixture["skip_reason"],
        }
    return result


def _fixture_record(config: IDPLiveSmokeConfig) -> dict[str, Any]:
    fixtures = _load_manifest()
    fixture = fixtures.get(config.fixture_id)
    fixture_root = PDF_ROOT
    if fixture is None:
        fixture = _load_smoke_manifest().get(config.fixture_id)
        fixture_root = SMOKE_ROOT
    if fixture is None:
        raise LiveIDPPreflightError("fixture_id_not_allowlisted")
    if config.fixture_path.is_symlink():
        raise LiveIDPPreflightError("fixture_path_invalid")
    path = config.fixture_path.resolve()
    try:
        path.relative_to(fixture_root)
    except ValueError as exc:
        raise LiveIDPPreflightError("fixture_path_out_of_scope") from exc
    if path.is_symlink() or path.name != fixture["filename"] or not path.is_file():
        raise LiveIDPPreflightError("fixture_path_invalid")
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != fixture["sha256"]:
        raise LiveIDPPreflightError("fixture_hash_mismatch")
    if len(body) > 20 * 1024 * 1024:
        raise LiveIDPPreflightError("fixture_size_exceeds_idp_limit")
    if "size_bytes" in fixture and len(body) != fixture["size_bytes"]:
        raise LiveIDPPreflightError("fixture_size_mismatch")
    return {**fixture, "path": str(path), "size_bytes": len(body)}


def validate_limits(
    observed: Mapping[str, object],
    *,
    limits: IDPLiveSmokeLimits = IDPLiveSmokeLimits(),
) -> None:
    """Reject unsafe provider reports before accepting a live result."""

    def bounded_int(key: str, maximum: int) -> int:
        if key not in observed:
            raise LiveIDPPreflightError(f"{key}_budget_missing")
        value = observed.get(key)
        if type(value) is not int or value < 0 or value > maximum:
            raise LiveIDPPreflightError(f"{key}_budget_exceeded_or_missing")
        return value

    bounded_int("modelCalls", limits.model_calls)
    bounded_int("inputTokens", limits.input_tokens)
    bounded_int("outputTokens", limits.output_tokens)
    bounded_int("ocrPages", limits.ocr_pages)
    bounded_int("ocrApiCalls", limits.ocr_api_calls)
    bounded_int("pollIterations", limits.poll_iterations)
    if "wallSeconds" not in observed:
        raise LiveIDPPreflightError("wall_time_budget_missing")
    wall = observed.get("wallSeconds")
    if not isinstance(wall, (int, float)) or isinstance(wall, bool) or wall < 0 or wall > limits.wall_seconds:
        raise LiveIDPPreflightError("wall_time_budget_exceeded_or_missing")
    if "retryCount" not in observed:
        raise LiveIDPPreflightError("retry_count_missing")
    retry_count = observed.get("retryCount")
    if type(retry_count) is not int or retry_count != limits.retry_count:
        raise LiveIDPPreflightError("automatic_retry_detected")
    if "ambiguousPaidOutcome" not in observed or "paidRetryAttempted" not in observed:
        raise LiveIDPPreflightError("paid_ambiguity_flags_missing")
    if observed.get("ambiguousPaidOutcome") is True and observed.get("paidRetryAttempted") is True:
        raise LiveIDPPreflightError("ambiguous_paid_call_replayed")
    stages = observed.get("stages")
    if not isinstance(stages, list):
        raise LiveIDPPreflightError("stage_ledger_missing")
    for stage in stages:
        if not isinstance(stage, Mapping):
            raise LiveIDPPreflightError("stage_ledger_invalid")
        if stage.get("ambiguous") is True and stage.get("attempts", 0) != 1:
            raise LiveIDPPreflightError("ambiguous_stage_replayed")
        if stage.get("state") == "IN_FLIGHT" and stage.get("paidCallCompleted") is True:
            raise LiveIDPPreflightError("inflight_paid_stage_unresolved")


def validate_browser_report(report: Mapping[str, object]) -> None:
    """Validate only runner-observable browser bounds, never provider claims."""

    wall = report.get("wallSeconds")
    if type(wall) is not int or wall < 0 or wall > MAX_WALL_SECONDS:
        raise LiveIDPPreflightError("browser_wall_budget_exceeded_or_missing")
    polls = report.get("polls")
    if not isinstance(polls, Mapping) or type(polls.get("iterations")) is not int or polls["iterations"] < 0 or polls["iterations"] > MAX_POLL_ITERATIONS:
        raise LiveIDPPreflightError("browser_poll_budget_exceeded_or_missing")
    api_calls = report.get("apiCallCount")
    api_limit = report.get("apiCallLimit")
    if type(api_calls) is not int or type(api_limit) is not int or api_limit != MAX_BROWSER_API_CALLS or api_calls < 0 or api_calls > api_limit:
        raise LiveIDPPreflightError("browser_api_budget_exceeded_or_missing")


def parse_safe_child_progress(stdout: object) -> dict[str, object]:
    """Extract only cleanup IDs from a failed/timeout browser child."""

    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if not isinstance(stdout, str):
        return {}
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping) or value.get("smoke") != "phase14-idp-browser-progress":
            continue
        cleanup: dict[str, object] = {
            key: value[key]
            for key in ("conversationId", "sessionId", "documentId")
            if isinstance(value.get(key), str) and _SAFE_ID.fullmatch(value[key])
        }
        raw_scopes = value.get("scopes")
        if isinstance(raw_scopes, list):
            scopes = [
                {"conversationId": item["conversationId"], "sessionId": item["sessionId"]}
                for item in raw_scopes
                if isinstance(item, Mapping)
                and isinstance(item.get("conversationId"), str) and _SAFE_ID.fullmatch(item["conversationId"])
                and isinstance(item.get("sessionId"), str) and _SAFE_ID.fullmatch(item["sessionId"])
            ]
            if scopes:
                cleanup["scopes"] = scopes
        if cleanup:
            return {"cleanup": cleanup}
    return {}


def build_evaluation_export(report: Mapping[str, object]) -> dict[str, object]:
    """Build evaluate_idp-compatible metadata without copying document text."""

    provenance = report.get("provenance") if isinstance(report.get("provenance"), Mapping) else {}
    release_sha = provenance.get("releaseSha256") if isinstance(provenance.get("releaseSha256"), str) else None
    model_id = provenance.get("modelId") if isinstance(provenance.get("modelId"), str) else None
    prompt_version = provenance.get("promptVersion") if isinstance(provenance.get("promptVersion"), str) else None
    provenance_complete = bool(release_sha and model_id and prompt_version and provenance.get("status") == "COMPLETE")
    safe_provenance = {
        "kind": "aws_e2e" if provenance_complete else "unknown",
        "release_sha256": release_sha,
        "model_id": model_id,
        "prompt_version": prompt_version,
    }
    records: list[dict[str, object]] = []
    for raw_case in report.get("cases", ()) if isinstance(report.get("cases"), list) else ():
        if not isinstance(raw_case, Mapping):
            continue
        fields: dict[str, object] = {}
        raw_fields = raw_case.get("fields") if isinstance(raw_case.get("fields"), Mapping) else {}
        for name, raw_field in raw_fields.items():
            if not isinstance(name, str) or not isinstance(raw_field, Mapping):
                continue
            item = {key: raw_field[key] for key in ("presence", "origin", "acceptance", "evidence") if key in raw_field}
            if "valueDigest" in raw_field and isinstance(raw_field["valueDigest"], str) and _SHA256.fullmatch(raw_field["valueDigest"].lower()):
                item["valueDigest"] = raw_field["valueDigest"].lower()
            elif "value" in raw_field:
                item["valueDigest"] = hashlib.sha256(json.dumps(raw_field["value"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
            evidence = item.get("evidence")
            if isinstance(evidence, list):
                item["evidence"] = [
                    {
                        **({"page": anchor["page"]} if "page" in anchor else {}),
                        **({"quoteDigest": anchor["quoteDigest"]} if isinstance(anchor.get("quoteDigest"), str) and _SHA256.fullmatch(anchor["quoteDigest"].lower()) else ({"quoteDigest": hashlib.sha256(anchor["quote"].encode("utf-8")).hexdigest()} if isinstance(anchor.get("quote"), str) else {})),
                        **({"contentSha256": anchor["contentSha256"].lower()} if isinstance(anchor.get("contentSha256"), str) and _SHA256.fullmatch(anchor["contentSha256"].lower()) else {}),
                    }
                    for anchor in evidence if isinstance(anchor, Mapping)
                ]
            fields[name] = item
        raw_observed = raw_case.get("observed") if isinstance(raw_case.get("observed"), Mapping) else {}
        observed: dict[str, object] = {}
        for group, keys in (("tokens", ("input", "output")), ("ocr", ("pages", "api_calls"))):
            raw_group = raw_observed.get(group) if isinstance(raw_observed.get(group), Mapping) else {}
            safe_group = {key: raw_group[key] for key in keys if type(raw_group.get(key)) is int and 0 <= raw_group[key] <= 10_000_000}
            if safe_group:
                observed[group] = safe_group
        if isinstance(raw_observed.get("latency_ms"), (int, float)) and not isinstance(raw_observed.get("latency_ms"), bool) and 0 <= raw_observed["latency_ms"] <= 86_400_000:
            observed["latency_ms"] = raw_observed["latency_ms"]
        records.append({
            "fixtureId": raw_case.get("fixtureId"),
            "documentType": raw_case.get("documentType"),
            "documentSha256": raw_case.get("documentSha256") if isinstance(raw_case.get("documentSha256"), str) and _SHA256.fullmatch(raw_case["documentSha256"].lower()) else None,
            "status": raw_case.get("status"),
            "fields": fields,
            # Derived values can contain legal document data. The public
            # projection does not expose a server-owned derived artifact, so
            # never copy an untrusted mapping into the evaluation export.
            "derived": None,
            "observed": observed,
        })
    return {"provenance": safe_provenance, "results": records}


def preflight(config: IDPLiveSmokeConfig) -> dict[str, object]:
    fixture = _fixture_record(config)
    manifest = _load_manifest()
    corpus_ocr_pages = sum(int(item["page_count"]) for item in manifest.values())
    if corpus_ocr_pages > MAX_OCR_PAGES:
        raise LiveIDPPreflightError("fixture_corpus_ocr_budget_exceeded")
    if config.region != REGION:
        raise LiveIDPPreflightError("region_restricted")
    expected_source = fixture.get("expected_source") or config.expected_source
    expected_status = config.expected_status or str(fixture.get("expected_status") or "")
    field_name = config.field_name or str(fixture.get("field_name") or "")
    if expected_source not in {"IDP", "RAG", "NONE"}:
        raise LiveIDPPreflightError("expected_source_invalid")
    if expected_status not in {"", "IDP_SKIPPED", "IDP_FAILED"}:
        raise LiveIDPPreflightError("expected_status_invalid")
    if config.review_action not in {"none", "approve", "correct"}:
        raise LiveIDPPreflightError("review_action_invalid")
    if field_name and not _SAFE_ID.fullmatch(field_name):
        raise LiveIDPPreflightError("field_name_invalid")
    if config.review_field_name and not _SAFE_ID.fullmatch(config.review_field_name):
        raise LiveIDPPreflightError("review_field_name_invalid")
    if config.execute and expected_source != "NONE" and not field_name:
        raise LiveIDPPreflightError("field_name_required_for_selected_query")
    if not _ATTEMPT_ID.fullmatch(config.attempt_id):
        raise LiveIDPPreflightError("attempt_id_invalid")
    if config.execute:
        parsed = urlsplit(config.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
            raise LiveIDPPreflightError("public_origin_invalid")
        if not config.idp_host or "/" in config.idp_host or ":" in config.idp_host:
            raise LiveIDPPreflightError("idp_host_invalid")
    configured = {
        "baseUrl": bool(config.base_url),
        "idpHost": bool(config.idp_host),
        "userPool": bool(config.user_pool_id),
        "metadataTable": bool(config.table_name),
        "memory": bool(config.memory_id),
        "matter": bool(config.matter_id),
        "crossMatter": bool(config.cross_matter_id),
    }
    if config.execute and not all(configured.values()):
        raise LiveIDPPreflightError("deployment_inputs_missing")
    evaluation_cases = [
        {
            "fixtureId": item["id"],
            "documentType": item.get("expected_type"),
            "documentSha256": item["sha256"],
            "status": "NOT_EXECUTED",
            "fields": {},
            "derived": None,
            "observed": {},
        }
        for item in manifest.values()
    ]
    return {
        "runner": "legaldesk-phase14-idp-live-smoke",
        "runnerVersion": RUNNER_VERSION,
        "mode": "preflight",
        "awsCalls": 0,
        "region": config.region,
        "fixture": {key: fixture[key] for key in ("id", "filename", "sha256", "page_count", "size_bytes", "expected_type", "expected_status", "expected_source", "field_name", "question", "skip_reason") if key in fixture},
        "limits": asdict(IDPLiveSmokeLimits()),
        "fixtureCorpus": {"count": len(manifest), "wholePdfOcrPages": corpus_ocr_pages, "maxWholePdfOcrPages": MAX_OCR_PAGES},
        "deploymentInputsConfigured": configured,
        "workerBudget": {"status": "NOT_EXECUTED", "reason": "server_paid_usage_not_exposed_by_authorized_metadata_projection"},
        "history": {"status": "NOT_EXECUTED", "reason": "three-run same-document trigger is not a public runner capability"},
        "idempotencyReplay": {"status": "NOT_EXECUTED", "reason": "duplicate clean-event trigger is not a public runner capability"},
        "provenance": {"fixtureSha256": fixture["sha256"], "syntheticOnly": True},
        "cases": evaluation_cases,
        "expectedStatus": expected_status or None,
        "expectedSource": expected_source,
    }


def run_live(config: IDPLiveSmokeConfig) -> dict[str, object]:
    """Run one approved browser envelope; never obtains the machine secret."""

    if not config.execute:
        raise LiveIDPPreflightError("explicit_execution_required")
    preflight(config)
    # One parent deadline covers setup, browser work, and result persistence;
    # the child receives only the remaining time instead of a fresh budget.
    deadline = time.monotonic() + MAX_WALL_SECONDS
    report_path = config.report_path
    if report_path is None:
        raise LiveIDPPreflightError("report_path_required")
    destination = report_path.resolve()
    try:
        destination.relative_to((ROOT / "evals" / "results").resolve())
    except ValueError as exc:
        raise LiveIDPPreflightError("report_path_out_of_scope") from exc
    if destination.exists() or destination.is_symlink():
        raise LiveIDPPreflightError("report_must_be_create_only")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps({"runner": "legaldesk-phase14-idp-live-smoke", "status": "STARTED"}) + "\n")
    except FileExistsError as exc:
        raise LiveIDPPreflightError("report_must_be_create_only") from exc
    fixture = _fixture_record(config)
    expected_source = fixture.get("expected_source") or config.expected_source
    expected_status = config.expected_status or str(fixture.get("expected_status") or "")
    field_name = config.field_name or str(fixture.get("field_name") or "")
    question = str(fixture.get("question") or "")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover
        raise LiveIDPPreflightError("boto3_unavailable") from exc
    request_config = Config(retries={"total_max_attempts": 1, "mode": "standard"}, connect_timeout=10, read_timeout=60)
    session = boto3.Session(region_name=config.region)
    cognito = session.client("cognito-idp", config=request_config)
    table = session.resource("dynamodb", config=request_config).Table(config.table_name)
    core = session.client("bedrock-agentcore", config=request_config)
    budget = _DeadlineBudget(CallBudget(maximum=120), deadline)
    cleanup_errors: list[str] = []
    username = f"phase14-idp-{config.attempt_id}-{__import__('secrets').token_hex(5)}"
    password = "Q!8a" + __import__("secrets").token_urlsafe(24)
    user_id = f"usr_phase14_idp_{config.attempt_id}_{__import__('secrets').token_hex(4)}"
    subject: str | None = None
    matter_item: Mapping[str, object] | None = None
    original_membership: list[str] | None = None
    expected_membership: list[str] | None = None
    membership_added = False
    profile_created = False
    user_created = False
    memory_scopes: list[tuple[str, str]] = []
    baseline_state: set[tuple[str, str]] = set()
    report: dict[str, object] = {"result": "FAIL", "smoke": "phase14-idp-live-smoke", "category": "not_started"}

    def safe_error_type(exc: BaseException) -> str:
        return type(exc).__name__ if type(exc).__name__ in {"Error", "TimeoutError", "TypeError", "ReferenceError", "RangeError", "AssertionError", "SmokeFailure"} else "UnknownError"

    def parse_child(stdout: object) -> dict[str, object]:
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if not isinstance(stdout, str):
            return {"result": "FAIL", "category": "browser_report_missing", "errorType": "NoReport"}
        for line in reversed(stdout.splitlines()):
            try:
                value = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping) and value.get("smoke") == "phase14-idp-browser" and value.get("result") in {"PASS", "NOT_EXECUTED", "FAIL"}:
                return dict(value)
        return {"result": "FAIL", "category": "browser_report_invalid", "errorType": "InvalidReport"}

    try:
        raw_matter = budget.call("dynamodb", table.get_item, Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"}, ConsistentRead=True).get("Item")
        if not isinstance(raw_matter, Mapping) or raw_matter.get("matterId") != config.matter_id or not isinstance(raw_matter.get("tenantId"), str) or not isinstance(raw_matter.get("authorizedUserIds"), list) or not all(isinstance(item, str) for item in raw_matter["authorizedUserIds"]):
            raise LiveIDPPreflightError("matter_profile_invalid")
        matter_item = dict(raw_matter)
        original_membership = list(raw_matter["authorizedUserIds"])
        expected_membership = [*original_membership, user_id]
        tenant_id = str(raw_matter["tenantId"])
        response = budget.call("cognito", cognito.admin_create_user, UserPoolId=config.user_pool_id, Username=username, MessageAction="SUPPRESS", TemporaryPassword=password, UserAttributes=[{"Name": "email", "Value": username + "@example.invalid"}, {"Name": "email_verified", "Value": "true"}])
        user_created = True
        attrs = response.get("User", {}).get("Attributes", []) if isinstance(response, Mapping) else []
        subject = next((item.get("Value") for item in attrs if isinstance(item, Mapping) and item.get("Name") == "sub"), None)
        if not isinstance(subject, str) or not _SAFE_ID.fullmatch(subject):
            raise LiveIDPPreflightError("cognito_subject_invalid")
        budget.call("cognito", cognito.admin_set_user_password, UserPoolId=config.user_pool_id, Username=username, Password=password, Permanent=True)
        profile = {"pk": f"AUTH#USER#{subject}", "sk": "PROFILE", "entityType": "User", "userId": user_id, "verifiedSubject": subject, "tenantIds": [tenant_id], "roles": ["member"]}
        budget.call("dynamodb", table.put_item, Item=profile, ConditionExpression="attribute_not_exists(pk)")
        profile_created = True
        baseline_state = discover_subject_state(table, subject, budget=budget)
        if baseline_state:
            raise LiveIDPPreflightError("synthetic_subject_state_collision")
        budget.call("dynamodb", table.update_item, Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"}, UpdateExpression="SET authorizedUserIds = list_append(authorizedUserIds, :user)", ConditionExpression="attribute_exists(pk) AND authorizedUserIds = :original AND NOT contains(authorizedUserIds, :user_id)", ExpressionAttributeValues={":original": original_membership, ":user": [user_id], ":user_id": user_id})
        membership_added = True
        child_remaining = deadline - time.monotonic()
        if child_remaining <= 1:
            raise LiveIDPPreflightError("wall_deadline_exceeded_before_browser")
        child_timeout = max(1, min(900, int(child_remaining)))
        child_deadline_epoch_ms = int(time.time() * 1000 + child_remaining * 1000)
        child_env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_") and "SECRET" not in key.upper() and "TOKEN" not in key.upper()}
        child_env.update({"LEGALDESK_P14_BASE_URL": config.base_url.rstrip("/"), "LEGALDESK_P14_IDP_HOST": config.idp_host, "LEGALDESK_P14_USERNAME": username, "LEGALDESK_P14_PASSWORD": password, "LEGALDESK_P14_MATTER_ID": config.matter_id, "LEGALDESK_P14_CROSS_MATTER_ID": config.cross_matter_id, "LEGALDESK_IDP_FIXTURE_PATH": fixture["path"], "LEGALDESK_IDP_FIXTURE_ID": fixture["id"], "LEGALDESK_IDP_EXPECTED_SOURCE": expected_source, "LEGALDESK_IDP_FIELD_NAME": field_name, "LEGALDESK_IDP_REVIEW_FIELD": config.review_field_name, "LEGALDESK_IDP_QUESTION": question, "LEGALDESK_IDP_EXPECTED_STATUS": expected_status, "LEGALDESK_IDP_EXPECTED_SKIP_REASON": str(fixture.get("skip_reason") or ""), "LEGALDESK_IDP_EXPECTED_DOCUMENT_TYPE": str(fixture["expected_type"] or ""), "LEGALDESK_IDP_REVIEW_ACTION": config.review_action, "LEGALDESK_IDP_DEADLINE_EPOCH_MS": str(child_deadline_epoch_ms)})
        try:
            child = subprocess.run(["node", str(BROWSER_RUNNER)], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=child_timeout)
            report = parse_child(child.stdout)
            progress = parse_safe_child_progress(child.stdout)
            if isinstance(progress.get("cleanup"), Mapping) and not isinstance(report.get("cleanup"), Mapping):
                # A child can fail before returning its normal report. Preserve
                # only safe progress IDs so the parent can still clean up every
                # conversation/session it observed.
                report["cleanup"] = progress["cleanup"]
            child_timed_out = False
        except subprocess.TimeoutExpired as exc:
            report = {"result": "FAIL", "category": "browser_timeout", "errorType": "TimeoutError", **parse_safe_child_progress(exc.stdout)}
            child_timed_out = True
        if report.get("result") == "PASS":
            validate_browser_report(report)
        if isinstance(report.get("cleanup"), Mapping):
            cleanup = report["cleanup"]
            raw_scopes = cleanup.get("scopes")
            if isinstance(raw_scopes, list):
                for item in raw_scopes:
                    if isinstance(item, Mapping) and isinstance(item.get("conversationId"), str) and isinstance(item.get("sessionId"), str):
                        memory_scopes.append(derive_memory_scope(tenant_id=tenant_id, user_id=user_id, matter_id=config.matter_id, conversation_id=item["conversationId"], session_id=item["sessionId"]))
            else:
                conversation_id = cleanup.get("conversationId")
                session_id = cleanup.get("sessionId")
                if isinstance(conversation_id, str) and isinstance(session_id, str):
                    memory_scopes.append(derive_memory_scope(tenant_id=tenant_id, user_id=user_id, matter_id=config.matter_id, conversation_id=conversation_id, session_id=session_id))
        if child_timed_out or child.returncode != 0:
            raise LiveIDPPreflightError("browser_runner_failed")
    except Exception as exc:
        report = {**report, "result": "FAIL", "category": report.get("category", "live_smoke_failed"), "errorType": safe_error_type(exc)}
    finally:
        # Never let an exhausted work deadline prevent bounded cleanup of the
        # synthetic user and its scoped state. Cleanup has its own small,
        # independent budget and is reported explicitly if it cannot finish.
        work_budget = budget
        budget = _DeadlineBudget(CallBudget(maximum=40), time.monotonic() + 120)
        # Credential revocation and matter authorization restoration come first;
        # unbounded user-generated memory/state rows must never consume the
        # cleanup budget needed to remove access.
        if membership_added and original_membership is not None and expected_membership is not None:
            try:
                budget.call("dynamodb", table.update_item, Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"}, UpdateExpression="SET authorizedUserIds = :original", ConditionExpression="authorizedUserIds = :expected", ExpressionAttributeValues={":original": original_membership, ":expected": expected_membership})
            except Exception as exc:
                cleanup_errors.append("matter_restore:" + safe_error_type(exc))
        if profile_created and subject is not None:
            try:
                budget.call("dynamodb", table.delete_item, Key={"pk": f"AUTH#USER#{subject}", "sk": "PROFILE"}, ConditionExpression="attribute_exists(pk) AND verifiedSubject = :subject AND userId = :user_id", ExpressionAttributeValues={":subject": subject, ":user_id": user_id})
            except Exception as exc:
                cleanup_errors.append("profile_cleanup:" + safe_error_type(exc))
        if user_created:
            try:
                budget.call("cognito", cognito.admin_delete_user, UserPoolId=config.user_pool_id, Username=username)
            except Exception as exc:
                cleanup_errors.append("cognito_cleanup:" + safe_error_type(exc))
        if subject is not None:
            try:
                owned = discover_subject_state(table, subject, budget=budget) - baseline_state
                if matter_item is not None:
                    owned |= query_history_state(table, subject, str(matter_item["tenantId"]), config.matter_id, budget=budget)
                for pk, sk in sorted(owned):
                    item = budget.call("dynamodb", table.get_item, Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
                    if isinstance(item, Mapping) and item.get("subject") not in (None, subject):
                        raise LiveIDPPreflightError("state_subject_mismatch")
                    if isinstance(item, Mapping):
                        budget.call("dynamodb", table.delete_item, Key={"pk": pk, "sk": sk}, ConditionExpression="attribute_exists(pk)")
            except Exception as exc:
                cleanup_errors.append("state_cleanup:" + safe_error_type(exc))
        for memory_scope in dict.fromkeys(memory_scopes):
            try:
                events = budget.call("agentcore", core.list_events, memoryId=config.memory_id, actorId=memory_scope[0], sessionId=memory_scope[1], maxResults=100)
                if events.get("nextToken"):
                    raise LiveIDPPreflightError("memory_cleanup_pagination")
                for event in events.get("events", ()):
                    event_id = event.get("eventId") if isinstance(event, Mapping) else None
                    if not isinstance(event_id, str):
                        raise LiveIDPPreflightError("memory_event_invalid")
                    budget.call("agentcore", core.delete_event, memoryId=config.memory_id, actorId=memory_scope[0], sessionId=memory_scope[1], eventId=event_id)
            except Exception as exc:
                cleanup_errors.append("memory_cleanup:" + safe_error_type(exc))
    report["cleanupErrors"] = cleanup_errors
    report["providerRequestCount"] = work_budget.count
    report["cleanupProviderRequestCount"] = budget.count
    report["fixtureSha256"] = fixture["sha256"]
    report["provenance"] = report.get("provenance", {"fixtureSha256": fixture["sha256"], "syntheticOnly": True})
    cleanup = report.get("cleanup") if isinstance(report.get("cleanup"), Mapping) else {}
    report["syntheticRetention"] = {
        "status": "NOT_EXECUTED",
        "reason": "public runner has no scoped document/artifact deletion contract; operator cleanup is required",
        "documentId": cleanup.get("documentId") if isinstance(cleanup.get("documentId"), str) else None,
        "reviewTaskAndArtifacts": "NOT_EXECUTED",
    }
    if cleanup_errors and report.get("result") == "PASS":
        report["result"] = "FAIL"
        report["category"] = "cleanup_failed"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8", errors="strict")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-approved-once", action="store_true")
    parser.add_argument("--base-url", default=os.environ.get("LEGALDESK_PUBLIC_BASE_URL", ""))
    parser.add_argument("--idp-host", default=os.environ.get("LEGALDESK_COGNITO_DOMAIN_HOST", ""))
    parser.add_argument("--matter", dest="matter_id", required=True)
    parser.add_argument("--cross-matter", dest="cross_matter_id", required=True)
    parser.add_argument("--user-pool-id", default=os.environ.get("LEGALDESK_COGNITO_USER_POOL_ID", ""))
    parser.add_argument("--table-name", default=os.environ.get("LEGALDESK_METADATA_TABLE_NAME", ""))
    parser.add_argument("--memory-id", default=os.environ.get("LEGALDESK_MEMORY_ID", ""))
    parser.add_argument("--fixture-id", required=True)
    parser.add_argument("--fixture-path", type=Path, required=True)
    parser.add_argument("--expected-source", choices=("IDP", "RAG", "NONE"), default="IDP")
    parser.add_argument("--field-name", default="")
    parser.add_argument("--review-field-name", default="")
    parser.add_argument("--review-action", choices=("none", "approve", "correct"), default="none")
    parser.add_argument("--expected-status", choices=("", "IDP_SKIPPED", "IDP_FAILED"), default="")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--attempt-id", default="local-preflight")
    parser.add_argument("--report-path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = IDPLiveSmokeConfig(
        base_url=args.base_url, idp_host=args.idp_host, matter_id=args.matter_id,
        cross_matter_id=args.cross_matter_id, user_pool_id=args.user_pool_id,
        table_name=args.table_name, memory_id=args.memory_id, fixture_path=args.fixture_path,
        fixture_id=args.fixture_id, region=args.region, attempt_id=args.attempt_id,
        report_path=args.report_path, execute=args.execute_approved_once,
        expected_source=args.expected_source, field_name=args.field_name, review_field_name=args.review_field_name, review_action=args.review_action, expected_status=args.expected_status,
    )
    try:
        report = run_live(config) if config.execute else preflight(config)
        print(json.dumps(report, sort_keys=True))
        return 0 if not config.execute or report.get("result") == "PASS" else 1
    except LiveIDPPreflightError as exc:
        print(json.dumps({"runner": "legaldesk-phase14-idp-live-smoke", "mode": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IDPLiveSmokeConfig", "IDPLiveSmokeLimits", "LiveIDPPreflightError",
    "build_evaluation_export", "parse_safe_child_progress", "preflight", "validate_browser_report", "validate_limits",
]
