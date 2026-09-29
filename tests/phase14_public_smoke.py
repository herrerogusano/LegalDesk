"""Bounded authenticated Phase 14 public-browser smoke.

The default mode is a local preflight only.  ``--execute-approved-once`` is
the sole switch that permits provider calls and is intentionally never used by
the test suite.  The live path uses one synthetic Cognito user, one existing
fictional matter, at most one bounded chat, and a metadata-only report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BROWSER_RUNNER = ROOT / "tests" / "phase14_public_browser.cjs"
STATE_PREFIX = "LEGALDESK#P14#STATE#"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
SAFE_ERROR_TYPES = frozenset({"Error", "TimeoutError", "TypeError", "ReferenceError", "RangeError", "AssertionError", "UnknownError", "SmokeFailure"})


@dataclass(frozen=True)
class PublicSmokeConfig:
    base_url: str
    idp_host: str
    matter_id: str
    cross_matter_id: str
    question: str
    expected_fact: str
    user_pool_id: str = ""
    table_name: str = ""
    memory_id: str = ""
    region: str = "eu-west-1"
    attempt_id: str = "local-preflight"
    report_path: Path | None = None


def validate_config(config: PublicSmokeConfig, *, execute: bool = False) -> PublicSmokeConfig:
    parsed = urlsplit(config.base_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        or parsed.hostname.lower().endswith(".invalid")
    ):
        raise ValueError("base URL must be an exact non-loopback HTTPS origin")
    if not config.idp_host or "/" in config.idp_host or ":" in config.idp_host or not SAFE_ID.fullmatch(config.idp_host.replace(".", "-")):
        raise ValueError("idp_host must be a host name")
    if not SAFE_ID.fullmatch(config.matter_id) or not SAFE_ID.fullmatch(config.cross_matter_id) or config.matter_id == config.cross_matter_id:
        raise ValueError("matter selectors are invalid")
    if not isinstance(config.question, str) or not config.question.strip() or len(config.question) > 1_000:
        raise ValueError("question is invalid")
    if not isinstance(config.expected_fact, str) or not config.expected_fact.strip() or len(config.expected_fact) > 1_000:
        raise ValueError("expected_fact is invalid")
    if not ATTEMPT_ID.fullmatch(config.attempt_id):
        raise ValueError("attempt_id is invalid")
    if execute and not all((config.user_pool_id, config.table_name, config.memory_id)):
        raise ValueError("user_pool_id, table_name, and memory_id are required for execution")
    return config


def parse_browser_report(stdout: object) -> dict[str, object]:
    """Accept only the closed JSON line emitted by the child runner."""

    lines = stdout.splitlines() if isinstance(stdout, str) else []
    last_progress = None
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("smoke") == "phase14-public-browser-progress":
            phase = payload.get("phase")
            if last_progress is None and isinstance(phase, str) and SAFE_ID.fullmatch(phase):
                last_progress = phase
            continue
        if not isinstance(payload, dict) or payload.get("smoke") != "phase14-public-browser":
            continue
        if payload.get("result") not in {"PASS", "FAIL"}:
            continue
        if payload.get("result") == "PASS" and payload.get("phase") == "preflight":
            if payload.get("browserExecutable") is not True:
                continue
        elif payload.get("result") == "PASS":
            required = {
                "matterId", "indexedDocuments", "citationCount", "reviewsListed",
                "reviewsOpened", "crossMatterStatus", "auditEventCount", "logoutStatus",
            }
            if not required <= payload.keys():
                continue
            if not all(isinstance(payload.get(key), int) and not isinstance(payload.get(key), bool) for key in required - {"matterId"}):
                continue
            if payload.get("crossMatterStatus") != 403 or payload.get("logoutStatus") != 200:
                continue
        else:
            if not all(isinstance(payload.get(key), str) and payload[key] for key in ("phase", "step", "category", "errorType")):
                continue
        cleanup = payload.get("cleanup")
        if cleanup is not None and (
            not isinstance(cleanup, dict)
            or any(value is not None and (not isinstance(value, str) or not SAFE_ID.fullmatch(value)) for value in cleanup.values())
        ):
            continue
        diagnostics = payload.get("diagnostics")
        if diagnostics is not None:
            chat = diagnostics.get("chat") if isinstance(diagnostics, Mapping) else None
            if chat is not None and (
                not isinstance(chat, Mapping)
                or chat.get("operationStatus") not in {"ok", "error", "blocked", "documents_processing", "unknown"}
                or chat.get("evidenceStatus") not in {"answerable", "ambiguous", "insufficient_evidence", None}
                or not isinstance(chat.get("citationCount"), int)
                or not isinstance(chat.get("answerContainsExpected"), bool)
            ):
                continue
        return payload
    return {
        "result": "FAIL", "smoke": "phase14-public-browser", "phase": "startup",
        "step": last_progress or "browser_process", "category": "browser_report_missing" if not lines else "browser_report_invalid",
        "errorType": "NoReport" if not lines else "InvalidReport",
    }


def _safe_error_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if name in SAFE_ERROR_TYPES else "UnknownError"


def safe_report(report: Mapping[str, object], *, cleanup_errors: list[str], request_counts: Mapping[str, int]) -> dict[str, object]:
    """Drop child-only cleanup selectors and retain no sensitive fields."""

    allowed = {
        "result", "smoke", "phase", "step", "category", "errorType", "matterId",
        "indexedDocuments", "citationCount", "reviewsListed", "reviewsOpened",
        "crossMatterStatus", "auditEventCount", "logoutStatus",
        "diagnostics",
    }
    output = {key: value for key, value in report.items() if key in allowed}
    output["cleanupErrors"] = list(cleanup_errors)
    output["requestCounts"] = {str(key): int(value) for key, value in request_counts.items()}
    return output


class CallBudget:
    def __init__(self, maximum: int = 80) -> None:
        self.maximum = maximum
        self.count = 0
        self.by_service: dict[str, int] = {}

    def call(self, service: str, function: Any, **kwargs: object) -> Any:
        self.count += 1
        if self.count > self.maximum:
            raise RuntimeError("provider request budget exceeded")
        self.by_service[service] = self.by_service.get(service, 0) + 1
        return function(**kwargs)


def _state_key(kind: str, value: object, suffix: str) -> dict[str, str]:
    import hashlib

    material = str(value).encode("utf-8")
    return {"pk": f"{STATE_PREFIX}{hashlib.sha256(material).hexdigest()}", "sk": suffix}


def discover_subject_state(
    table: Any,
    subject: str,
    *,
    budget: CallBudget,
    limit: int = 100,
    max_scanned: int = 1_000,
) -> set[tuple[str, str]]:
    """Discover a subject's P14 state within explicit page and scan bounds."""

    from boto3.dynamodb.conditions import Attr

    result: set[tuple[str, str]] = set()
    scanned = 0
    start_key = None
    for _page in range(10):
        kwargs: dict[str, object] = {
            "FilterExpression": (Attr("subject").eq(subject) | Attr("verifiedSubject").eq(subject)) & Attr("pk").begins_with(STATE_PREFIX),
            "ProjectionExpression": "#pk,#sk,#entity",
            "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk", "#entity": "entityType"},
            "Limit": min(100, max_scanned - scanned),
        }
        if start_key is not None:
            kwargs["ExclusiveStartKey"] = start_key
        response = budget.call("dynamodb", table.scan, **kwargs)
        scanned += int(response.get("ScannedCount", 0))
        items = response.get("Items", ())
        if not isinstance(items, list) or len(result) + len(items) > limit:
            raise RuntimeError("subject state discovery exceeded result bound")
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("pk"), str) or not isinstance(item.get("sk"), str):
                raise RuntimeError("subject state key is malformed")
            result.add((item["pk"], item["sk"]))
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return result
        if scanned >= max_scanned:
            raise RuntimeError("subject state discovery exceeded scan bound")
    raise RuntimeError("subject state discovery exceeded page bound")


def query_history_state(table: Any, subject: str, tenant_id: str, matter_id: str, *, budget: CallBudget, limit: int = 100) -> set[tuple[str, str]]:
    from boto3.dynamodb.conditions import Key

    key = _state_key("HISTORY", (subject, tenant_id, matter_id), "unused")
    response = budget.call(
        "dynamodb", table.query,
        KeyConditionExpression=Key("pk").eq(key["pk"]) & Key("sk").begins_with("EVENT#"),
        ProjectionExpression="#pk,#sk",
        ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
        Limit=limit,
        ConsistentRead=True,
    )
    if response.get("LastEvaluatedKey"):
        raise RuntimeError("history state discovery was not complete")
    result = set()
    for item in response.get("Items", ()):
        if not isinstance(item, Mapping) or not isinstance(item.get("pk"), str) or not isinstance(item.get("sk"), str):
            raise RuntimeError("history state key is malformed")
        result.add((item["pk"], item["sk"]))
    return result


def derive_memory_scope(*, tenant_id: str, user_id: str, matter_id: str, conversation_id: str, session_id: str) -> tuple[str, str]:
    import hashlib
    actor_material = "\x1f".join(("legaldesk-memory-v1", tenant_id, user_id, matter_id)).encode("utf-8")
    actor = f"ldactor-{hashlib.sha256(actor_material).hexdigest()[:48]}"
    session_material = "\x1f".join(("legaldesk-memory-session-v1", actor, conversation_id, session_id)).encode("utf-8")
    raw = hashlib.sha256(session_material).digest()[:16]
    import uuid
    return actor, str(uuid.UUID(bytes=raw))


def run_live(config: PublicSmokeConfig) -> tuple[dict[str, object], int]:
    """Run one explicitly approved smoke and always attempt bounded cleanup."""

    import boto3
    from botocore.config import Config
    request_config = Config(retries={"total_max_attempts": 1, "mode": "standard"}, connect_timeout=10, read_timeout=60)
    session = boto3.Session(region_name=config.region)
    cognito = session.client("cognito-idp", config=request_config)
    table = session.resource("dynamodb", config=request_config).Table(config.table_name)
    core = session.client("bedrock-agentcore", config=request_config)
    budget = CallBudget()
    cleanup_errors: list[str] = []
    request_counts: dict[str, int] = {}
    username = f"phase14-tech-{config.attempt_id}-{secrets.token_hex(5)}"
    password = "Q!8a" + secrets.token_urlsafe(24)
    subject = None
    user_id = f"usr_phase14_{config.attempt_id}_{secrets.token_hex(4)}"
    matter_item: dict[str, object] | None = None
    original_membership: list[str] | None = None
    expected_membership: list[str] | None = None
    membership_added = False
    profile_created = False
    user_created = False
    baseline_state: set[tuple[str, str]] = set()
    memory_scope: tuple[str, str] | None = None
    report: dict[str, object] = {"result": "FAIL", "smoke": "phase14-public-browser", "phase": "startup", "step": "startup", "category": "not_started", "errorType": "UnknownError"}

    def record_error(label: str, exc: BaseException | None = None) -> None:
        suffix = _safe_error_type(exc) if exc else "Unsafe"
        cleanup_errors.append(f"{label}:{suffix}")

    try:
        matter_response = budget.call("dynamodb", table.get_item, Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"}, ConsistentRead=True)
        raw_matter = matter_response.get("Item") if isinstance(matter_response, Mapping) else None
        if not isinstance(raw_matter, Mapping) or raw_matter.get("matterId") != config.matter_id or not isinstance(raw_matter.get("tenantId"), str) or not isinstance(raw_matter.get("authorizedUserIds"), list) or not all(isinstance(value, str) for value in raw_matter["authorizedUserIds"]):
            raise RuntimeError("matter profile is malformed")
        matter_item = dict(raw_matter)
        original_membership = list(raw_matter["authorizedUserIds"])
        tenant_id = str(raw_matter["tenantId"])
        if user_id in original_membership:
            raise RuntimeError("generated user id collided with matter membership")
        expected_membership = [*original_membership, user_id]

        preflight = subprocess.run(
            ["node", str(BROWSER_RUNNER), "--preflight"], cwd=ROOT,
            env={key: value for key, value in os.environ.items() if not key.startswith("AWS_")},
            capture_output=True, text=True, timeout=60,
        )
        if preflight.returncode != 0 or parse_browser_report(preflight.stdout).get("result") != "PASS":
            raise RuntimeError("browser runtime preflight failed")

        response = budget.call(
            "cognito", cognito.admin_create_user,
            UserPoolId=config.user_pool_id, Username=username, MessageAction="SUPPRESS",
            TemporaryPassword=password,
            UserAttributes=[{"Name": "email", "Value": username + "@example.invalid"}, {"Name": "email_verified", "Value": "true"}],
        )
        user_created = True
        attrs = ((response.get("User") or {}).get("Attributes") if isinstance(response, Mapping) else None)
        subject = next((item.get("Value") for item in attrs or () if isinstance(item, Mapping) and item.get("Name") == "sub"), None)
        if not isinstance(subject, str) or not SAFE_ID.fullmatch(subject):
            raise RuntimeError("Cognito subject is malformed")
        budget.call("cognito", cognito.admin_set_user_password, UserPoolId=config.user_pool_id, Username=username, Password=password, Permanent=True)
        profile = {"pk": f"AUTH#USER#{subject}", "sk": "PROFILE", "entityType": "User", "userId": user_id, "verifiedSubject": subject, "tenantIds": [tenant_id], "roles": ["member"]}
        budget.call("dynamodb", table.put_item, Item=profile, ConditionExpression="attribute_not_exists(pk)")
        profile_created = True
        baseline_state = discover_subject_state(table, subject, budget=budget)
        if baseline_state:
            raise RuntimeError("new technical subject already has state")
        budget.call(
            "dynamodb", table.update_item,
            Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"},
            UpdateExpression="SET authorizedUserIds = list_append(authorizedUserIds, :user)",
            ConditionExpression="attribute_exists(pk) AND authorizedUserIds = :original AND NOT contains(authorizedUserIds, :user_id)",
            ExpressionAttributeValues={":original": original_membership, ":user": [user_id], ":user_id": user_id},
        )
        membership_added = True

        child_env = dict(os.environ)
        child_env.update({
            "LEGALDESK_P14_BASE_URL": config.base_url.rstrip("/"), "LEGALDESK_P14_IDP_HOST": config.idp_host,
            "LEGALDESK_P14_USERNAME": username, "LEGALDESK_P14_PASSWORD": password,
            "LEGALDESK_P14_MATTER_ID": config.matter_id, "LEGALDESK_P14_CROSS_MATTER_ID": config.cross_matter_id,
            "LEGALDESK_P14_QUESTION": config.question, "LEGALDESK_P14_EXPECTED_FACT": config.expected_fact,
        })
        for key in list(child_env):
            if key.startswith("AWS_"):
                child_env.pop(key)
        child = subprocess.run(["node", str(BROWSER_RUNNER)], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=900)
        del child_env
        report = parse_browser_report(child.stdout)
        request_counts = report.get("requestCounts", {}) if isinstance(report.get("requestCounts"), Mapping) else {}
        cleanup_info = report.get("cleanup") if isinstance(report.get("cleanup"), Mapping) else {}
        conversation_id = cleanup_info.get("conversationId")
        session_id = cleanup_info.get("sessionId")
        if isinstance(conversation_id, str) and isinstance(session_id, str):
            memory_scope = derive_memory_scope(tenant_id=tenant_id, user_id=user_id, matter_id=config.matter_id, conversation_id=conversation_id, session_id=session_id)
        if child.returncode != 0 or report.get("result") != "PASS":
            raise RuntimeError("public browser smoke failed")
    except Exception as exc:
        report = {**report, "result": "FAIL", "category": report.get("category", "smoke_failed"), "errorType": _safe_error_type(exc)}
    finally:
        if core is not None and memory_scope is not None:
            try:
                events = budget.call("agentcore", core.list_events, memoryId=config.memory_id, actorId=memory_scope[0], sessionId=memory_scope[1], maxResults=100)
                if events.get("nextToken"):
                    raise RuntimeError("memory pagination exceeded bound")
                for event in events.get("events", ()):
                    event_id = event.get("eventId") if isinstance(event, Mapping) else None
                    if not isinstance(event_id, str) or not event_id:
                        raise RuntimeError("memory event id malformed")
                    budget.call("agentcore", core.delete_event, memoryId=config.memory_id, actorId=memory_scope[0], sessionId=memory_scope[1], eventId=event_id)
            except Exception as exc:
                record_error("memory_cleanup", exc)
        if subject is not None:
            try:
                discovered = discover_subject_state(table, subject, budget=budget)
                history = query_history_state(table, subject, str(matter_item["tenantId"]), config.matter_id, budget=budget) if matter_item else set()
                owned = (discovered | history) - baseline_state
                for pk, sk in sorted(owned):
                    current = budget.call("dynamodb", table.get_item, Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
                    if isinstance(current, Mapping) and current.get("subject") not in (None, subject):
                        raise RuntimeError("state subject mismatch")
                    if isinstance(current, Mapping):
                        budget.call("dynamodb", table.delete_item, Key={"pk": pk, "sk": sk}, ConditionExpression="attribute_exists(pk)")
            except Exception as exc:
                record_error("state_cleanup", exc)
        if membership_added and matter_item is not None and original_membership is not None and expected_membership is not None:
            try:
                budget.call("dynamodb", table.update_item, Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"}, UpdateExpression="SET authorizedUserIds = :original", ConditionExpression="authorizedUserIds = :expected", ExpressionAttributeValues={":original": original_membership, ":expected": expected_membership})
                verify = budget.call("dynamodb", table.get_item, Key={"pk": f"AUTH#MATTER#{config.matter_id}", "sk": "PROFILE"}, ConsistentRead=True).get("Item")
                if not isinstance(verify, Mapping) or verify.get("authorizedUserIds") != original_membership:
                    raise RuntimeError("matter restoration verification failed")
            except Exception as exc:
                record_error("matter_restore", exc)
        if profile_created and subject is not None:
            try:
                budget.call("dynamodb", table.delete_item, Key={"pk": f"AUTH#USER#{subject}", "sk": "PROFILE"}, ConditionExpression="attribute_exists(pk) AND verifiedSubject = :subject AND userId = :user_id", ExpressionAttributeValues={":subject": subject, ":user_id": user_id})
            except Exception as exc:
                record_error("profile_cleanup", exc)
        if user_created:
            try:
                budget.call("cognito", cognito.admin_delete_user, UserPoolId=config.user_pool_id, Username=username)
            except Exception as exc:
                record_error("cognito_cleanup", exc)

    final = safe_report(report, cleanup_errors=cleanup_errors, request_counts=request_counts)
    final["providerRequestCount"] = budget.count
    final["cleanupErrors"] = cleanup_errors
    return final, 0 if final.get("result") == "PASS" and not cleanup_errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-approved-once", action="store_true")
    parser.add_argument("--base-url", default=os.environ.get("LEGALDESK_PUBLIC_BASE_URL", ""))
    parser.add_argument("--idp-host", default=os.environ.get("LEGALDESK_COGNITO_DOMAIN_HOST", ""))
    parser.add_argument("--matter", dest="matter_id", default="")
    parser.add_argument("--cross-matter", dest="cross_matter_id", default="")
    parser.add_argument("--question", default="")
    parser.add_argument("--expected-fact", default="")
    parser.add_argument("--user-pool-id", default=os.environ.get("LEGALDESK_COGNITO_USER_POOL_ID", ""))
    parser.add_argument("--table-name", default=os.environ.get("LEGALDESK_METADATA_TABLE_NAME", ""))
    parser.add_argument("--memory-id", default=os.environ.get("LEGALDESK_MEMORY_ID", ""))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--attempt-id", default="local-preflight")
    parser.add_argument("--report-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = PublicSmokeConfig(args.base_url, args.idp_host, args.matter_id, args.cross_matter_id, args.question, args.expected_fact, args.user_pool_id, args.table_name, args.memory_id, args.region, args.attempt_id, Path(args.report_path) if args.report_path else None)
    try:
        validate_config(config, execute=args.execute_approved_once)
        if not args.execute_approved_once:
            print(json.dumps({"mode": "LOCAL_PREFLIGHT", "smoke": "phase14-public-browser", "awsCalls": 0, "baseUrlConfigured": bool(config.base_url), "matterConfigured": bool(config.matter_id), "questionConfigured": bool(config.question), "expectedFactConfigured": bool(config.expected_fact)}))
            return 0
        if config.report_path is None:
            raise ValueError("--report-path is required for execution")
        if config.report_path.exists():
            raise ValueError("refusing to overwrite existing report")
        config.report_path.parent.mkdir(parents=True, exist_ok=True)
        result, status = run_live(config)
        config.report_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8", errors="strict")
        print(json.dumps({"result": result.get("result"), "smoke": result.get("smoke"), "cleanupErrors": result.get("cleanupErrors", []), "providerRequestCount": result.get("providerRequestCount")}))
        return status
    except Exception as exc:
        print(json.dumps({"result": "FAIL", "smoke": "phase14-public-browser", "category": "preflight", "errorType": _safe_error_type(exc), "awsCalls": 0}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
