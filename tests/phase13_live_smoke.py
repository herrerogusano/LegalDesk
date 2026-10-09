"""One explicitly authorized real-browser smoke; never a default test.

Operator credentials provision/clean synthetic fixtures only. The application
receives an STS restricted session. Credentials/passwords stay in memory.
Infrastructure teardown is separate and must follow the execution ledger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend/src"), str(ROOT / "agent/src")]
REGION = "eu-west-1"
ACCOUNT = "344774635844"
TENANT = "tnt_phase13_20260921"
MATTERS = ("mat_phase13_a_20260921", "mat_phase13_b_20260921")
TABLE = "LegalDeskPhase02Documents-DocumentMetadataTable-1DANFLTX8RW7T"
BUCKET = "legaldeskphase02documents-documentbucket-ojlu4kvhlied"
POOL = "eu-west-1_tHvFPpktv"
MEMORY = "LegalDeskPhase09-NKV8SZFz5U"
HARNESS = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/LegalDeskPhase01-7EMjvNs1PC"
MODEL = "eu.anthropic.claude-sonnet-4-6"
KNOWLEDGE_BASE_ID = "40R8OKAZOR"
DATA_SOURCE_ID = "A53UDNNEMP"
GUARDRAIL_IDENTIFIER = "qin0b7t7vmtd"
GUARDRAIL_VERSION = "1"
CLIENT_ID = "101hke40t7n5easmh7i3g9265o"
REPORT = ROOT / "build/phase13-smoke/live-result.json"  # historical sentinel
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def parse_browser_report(stdout, *, returncode=0):
    """Parse only the browser runner's closed JSON envelope.

    The browser process can fail before loading its module, in which case it
    may emit no stdout at all.  Never retain stderr or an arbitrary output
    line: the smoke report must contain only a safe closed diagnostic.
    """
    del returncode
    lines = stdout.splitlines() if isinstance(stdout, str) else []
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("smoke") == "phase13-live-browser"
            and payload.get("result") in {"PASS", "FAIL"}
        ):
            return payload
    return {
        "result": "FAIL",
        "smoke": "phase13-live-browser",
        "phase": "startup",
        "step": "browser_process",
        "category": "browser_report_missing" if not lines else "browser_report_invalid",
        "errorType": "NoReport" if not lines else "InvalidReport",
    }


def browser_runtime_preflight(*, runner=subprocess.run):
    """Verify Node, Playwright and the browser executable before AWS writes."""
    child_env = dict(os.environ)
    for key in list(child_env):
        if key.startswith("AWS_"):
            child_env.pop(key)
    try:
        completed = runner(
            ["node", str(ROOT / "tests/phase13_live_browser.cjs"), "--preflight"],
            cwd=ROOT,
            env=child_env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("browser runtime preflight failed") from exc
    report = parse_browser_report(completed.stdout, returncode=completed.returncode)
    if completed.returncode != 0 or report.get("result") != "PASS" or report.get("phase") != "preflight":
        raise RuntimeError("browser runtime preflight failed")
    return report


def session_policy(subjects, *, knowledge_base_id=KNOWLEDGE_BASE_ID, guardrail_identifier=GUARDRAIL_IDENTIFIER):
    """Narrow resource/action intersections; never IAM/control-plane authority."""
    bedrock = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:"
    agentcore = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:"
    def allow(actions, resources, **extra):
        return dict(Effect="Allow", Action=actions, Resource=resources, **extra)
    statements = [
        allow(["s3:PutObject", "s3:GetObject"], f"arn:aws:s3:::{BUCKET}/tenants/{TENANT}/matters/*/documents/*"),
        allow(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
              f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{TABLE}",
              Condition={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [
                  *[f"AUTH#USER#{sub}" for sub in subjects], *[f"AUTH#MATTER#{m}" for m in MATTERS],
                  f"TENANT#{TENANT}#MATTER#*", f"CONVERSATION#{TENANT}#*", "GATEWAY#INVOCATION#*", "GATEWAY#GRANT#*",
              ]}}),
        allow(["bedrock:Retrieve", "bedrock:StartIngestionJob", "bedrock:GetIngestionJob"], bedrock + "knowledge-base/" + knowledge_base_id),
        allow("bedrock:ApplyGuardrail", bedrock + "guardrail/" + guardrail_identifier),
        allow("bedrock:InvokeModel", [bedrock + "inference-profile/" + MODEL,
              "arn:aws:bedrock:eu-*::foundation-model/anthropic.claude-sonnet-4-6"]),
        allow(["bedrock-agentcore:InvokeHarness", "bedrock-agentcore:InvokeAgentRuntime"], HARNESS),
        allow(["bedrock-agentcore:CreateEvent", "bedrock-agentcore:ListEvents"], agentcore + "memory/" + MEMORY),
    ]
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))


class TrackingProxy:
    def __init__(self, delegate, state, kind):
        self.delegate, self.state, self.kind = delegate, state, kind

    def __getattr__(self, name):
        method = getattr(self.delegate, name)
        if not callable(method):
            return method
        def call(*args, **kwargs):
            if self.kind == "table" and name in {"put_item", "update_item"}:
                item = kwargs.get("Item", kwargs.get("Key", {}))
                self.state["keys"].add((item["pk"], item["sk"]))
            if self.kind == "s3":
                params = kwargs.get("Params", kwargs)
                if params.get("Key"):
                    self.state["objects"].add(params["Key"])
            if self.kind == "bedrock-agentcore":
                actor = kwargs.get("actorId")
                session = kwargs.get("sessionId", kwargs.get("runtimeSessionId"))
                if actor and session:
                    self.state["memory_scopes"].add((actor, session))
            return method(*args, **kwargs)
        return call


class TrackingSession:
    def __init__(self, session, state):
        self.session, self.state = session, state

    def client(self, service, **kwargs):
        return TrackingProxy(self.session.client(service, **kwargs), self.state, service)

    def resource(self, service, **kwargs):
        resource = self.session.resource(service, **kwargs)
        state = self.state
        class Resource:
            def Table(self, name):
                return TrackingProxy(resource.Table(name), state, "table")
        return Resource()


class _OfflineProvider:
    """Constructor-only provider double; any operation is a preflight failure."""

    def __getattr__(self, name):
        raise AssertionError(f"offline preflight attempted provider operation: {name}")


class _OfflineTable(_OfflineProvider):
    """Resource-shaped table seam for constructor-only composition checks.

    Boto3 resource tables expose ``meta.client``.  Supplying that shape keeps
    repository construction local; invoking any actual Dynamo operation still
    fails closed through ``_OfflineProvider``.
    """

    def __init__(self):
        self.meta = SimpleNamespace(client=_OfflineProvider())


class _OfflineSession:
    """Scoped local session used to build the exact composition without AWS."""

    def __init__(self):
        self.client_services = []
        self.resource_services = []
        self.table = _OfflineTable()

    def client(self, service, **kwargs):
        self.client_services.append(service)
        return _OfflineProvider()

    def resource(self, service, **kwargs):
        self.resource_services.append(service)
        table = self.table

        class Resource:
            def Table(self, _name):
                return table

        return Resource()


class _OfflineJwks:
    def get_signing_key(self, _token):
        raise AssertionError("offline preflight attempted JWKS resolution")


def validate_smoke_refs(*, attempt_id, knowledge_base_id, data_source_id, guardrail_identifier, guardrail_version):
    if _ATTEMPT_ID.fullmatch(attempt_id or "") is None:
        raise ValueError("attempt-id must be a bounded identifier")
    for name, value in {
        "knowledge-base-id": knowledge_base_id,
        "data-source-id": data_source_id,
        "guardrail-identifier": guardrail_identifier,
        "guardrail-version": guardrail_version,
    }.items():
        if _ID.fullmatch(value or "") is None:
            raise ValueError(f"{name} must be a bounded identifier")


def make_resource_config(*, knowledge_base_id=KNOWLEDGE_BASE_ID, data_source_id=DATA_SOURCE_ID,
                         guardrail_identifier=GUARDRAIL_IDENTIFIER, guardrail_version=GUARDRAIL_VERSION):
    from legaldesk.application import AWSResourceConfig

    issuer = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
    domain = "https://legaldesk-phase08-344774635844.auth.eu-west-1.amazoncognito.com"
    return AWSResourceConfig(
        region=REGION, metadata_table_name=TABLE, source_bucket_name=BUCKET,
        knowledge_base_id=knowledge_base_id, data_source_id=data_source_id,
        guardrail_identifier=guardrail_identifier, guardrail_version=guardrail_version,
        resolver_model_id=MODEL, writer_model_id=MODEL, harness_arn=HARNESS,
        gateway_url="https://legaldeskgatewayphase08-f17ddi2woq.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp",
        memory_id=MEMORY, issuer=issuer, jwks_url=issuer + "/.well-known/jwks.json",
        client_id=CLIENT_ID, authorization_endpoint=domain + "/oauth2/authorize",
        token_endpoint=domain + "/oauth2/token", matter_catalog=MATTERS,
    )


def offline_factory_preflight(config):
    """Build exact application wiring with no network-capable provider object."""
    from legaldesk.application import build_aws_composition
    from legaldesk.smoke_budget import SmokeBudget

    session = _OfflineSession()
    with patch("legaldesk.application.PyJwtJwksKeyResolver", return_value=_OfflineJwks()):
        composition = build_aws_composition(
            allow_aws=True, config=config, smoke_budget=SmokeBudget(), boto3_session=session,
        )
    if composition.identity_verifier.config.allowed_token_use != frozenset({"access"}):
        raise AssertionError("composition must accept only OAuth access tokens")
    return {
        "providerClientConstructors": tuple(session.client_services),
        "providerResourceConstructors": tuple(session.resource_services),
        "allowedTokenUse": ("access",),
        "audienceConfigured": config.audience is not None,
    }


def resolve_report_path(attempt_id, supplied_path=None):
    report_path = Path(supplied_path) if supplied_path else ROOT / "build/phase13-smoke" / f"live-result-{attempt_id}.json"
    report_path = report_path if report_path.is_absolute() else ROOT / report_path
    report_path = report_path.resolve()
    if report_path == REPORT.resolve():
        raise RuntimeError("new smoke report must not reuse the historical sentinel")
    if attempt_id not in report_path.stem:
        raise RuntimeError("new smoke report filename must include the attempt id")
    if report_path.exists():
        raise RuntimeError("refusing to overwrite an existing smoke report")
    return report_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-approved-once", action="store_true")
    parser.add_argument("--attempt-id")
    parser.add_argument("--knowledge-base-id")
    parser.add_argument("--data-source-id")
    parser.add_argument("--guardrail-identifier")
    parser.add_argument("--guardrail-version", default=GUARDRAIL_VERSION)
    parser.add_argument("--report-path")
    args = parser.parse_args()
    if args.execute_approved_once and not all((args.attempt_id, args.knowledge_base_id, args.data_source_id, args.guardrail_identifier)):
        parser.error("--execute-approved-once requires --attempt-id, --knowledge-base-id, --data-source-id and --guardrail-identifier")
    attempt_id = args.attempt_id or "local-preflight"
    knowledge_base_id = args.knowledge_base_id or KNOWLEDGE_BASE_ID
    data_source_id = args.data_source_id or DATA_SOURCE_ID
    guardrail_identifier = args.guardrail_identifier or GUARDRAIL_IDENTIFIER
    guardrail_version = args.guardrail_version
    validate_smoke_refs(
        attempt_id=attempt_id, knowledge_base_id=knowledge_base_id,
        data_source_id=data_source_id, guardrail_identifier=guardrail_identifier,
        guardrail_version=guardrail_version,
    )
    policy = session_policy(
        ["a" * 36, "b" * 36], knowledge_base_id=knowledge_base_id,
        guardrail_identifier=guardrail_identifier,
    )
    if len(policy) > 2048:
        raise RuntimeError("Session policy exceeds STS inline limit")
    pdf = ROOT / "output/pdf/fictional-storage-note.pdf"
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != "03de0479544250393d9709a1eb4511a1e24332871f06252b8cb4513398d0c17a":
        raise RuntimeError("Frozen smoke PDF has changed")
    resource_config = make_resource_config(
        knowledge_base_id=knowledge_base_id, data_source_id=data_source_id,
        guardrail_identifier=guardrail_identifier, guardrail_version=guardrail_version,
    )
    if not args.execute_approved_once:
        factory = offline_factory_preflight(resource_config)
        print(json.dumps({
            "mode": "LOCAL_PREFLIGHT", "attemptId": attempt_id,
            "explicitToolDispatch": "direct_gateway_tools_call",
            "policyCharacters": len(policy), "awsCalls": 0,
            "resourceRefs": {
                "knowledgeBaseId": knowledge_base_id,
                "dataSourceId": data_source_id,
                "guardrailIdentifier": guardrail_identifier,
                "guardrailVersion": guardrail_version,
            },
            "factory": factory,
        }))
        return 0
    # Fail locally before importing AWS clients or creating synthetic users.
    browser_runtime_preflight()
    report_path = resolve_report_path(attempt_id, args.report_path)
    import boto3
    from boto3.dynamodb.conditions import Attr, Key
    from botocore.config import Config
    from legaldesk.application import build_aws_composition
    from legaldesk.__main__ import QuietRequestHandler
    from legaldesk.http_app import create_http_app
    from legaldesk.smoke_budget import SmokeBudget
    cfg = Config(retries={"total_max_attempts": 1, "mode": "standard"})
    operator = boto3.Session(region_name=REGION)
    cognito = operator.client("cognito-idp", config=cfg)
    table = operator.resource("dynamodb", config=cfg).Table(TABLE)
    s3 = operator.client("s3", config=cfg)
    core = operator.client("bedrock-agentcore", config=cfg)
    budget = SmokeBudget()
    state = {"keys": set(), "objects": set(), "memory_scopes": set()}
    created_users, subjects, passwords = [], [], []
    server = thread = None
    result = {
        "status": "STARTED", "attemptId": attempt_id,
        # Explicit metadata/review buttons use deterministic backend→Gateway
        # MCP calls; Harness remains reserved for future agentic workflows.
        "explicitToolDispatch": "direct_gateway_tools_call",
        "resourceRefs": {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": data_source_id,
            "guardrailIdentifier": guardrail_identifier,
            "guardrailVersion": guardrail_version,
        },
        "cleanupErrors": [], "checks": [],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive sentinel prevents even an interrupted invocation being rerun.
    with report_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream)
    def save():
        result["budget"] = asdict(budget.snapshot())
        result["syntheticUsers"] = list(created_users)
        result["cleanupKeys"] = [dict(pk=pk, sk=sk) for pk, sk in sorted(state["keys"])]
        result["objectKeys"] = sorted(state["objects"])
        result["memoryScopes"] = sorted(state["memory_scopes"])
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    try:
        for i, suffix in enumerate(("a", "b")):
            username = f"phase13-smoke-{attempt_id}-{suffix}"
            password = "Q!8a" + secrets.token_urlsafe(24)
            response = cognito.admin_create_user(UserPoolId=POOL, Username=username, MessageAction="SUPPRESS", TemporaryPassword=password,
                UserAttributes=[{"Name": "email", "Value": username + "@example.invalid"}, {"Name": "email_verified", "Value": "true"}])
            created_users.append(username)
            sub = next(item["Value"] for item in response["User"]["Attributes"] if item["Name"] == "sub")
            subjects.append(sub)
            passwords.append(password)
            save()
            cognito.admin_set_user_password(UserPoolId=POOL, Username=username, Password=password, Permanent=True)
            user_id = f"usr_phase13_{attempt_id}_{suffix}"
            items = [
                dict(pk=f"AUTH#USER#{sub}", sk="PROFILE", entityType="User", userId=user_id, verifiedSubject=sub, tenantIds=[TENANT], roles=["member"]),
                dict(pk=f"AUTH#MATTER#{MATTERS[i]}", sk="PROFILE", entityType="Matter", matterId=MATTERS[i], tenantId=TENANT, name=f"Synthetic smoke {suffix}", authorizedUserIds=[user_id], status="active"),
            ]
            for item in items:
                budget.call("dynamodb", table.put_item, Item=item, ConditionExpression="attribute_not_exists(pk)")
                state["keys"].add((item["pk"], item["sk"]))
                save()
        policy = session_policy(
            subjects, knowledge_base_id=knowledge_base_id,
            guardrail_identifier=guardrail_identifier,
        )
        if len(policy) > 2048:
            raise RuntimeError("Session policy exceeds limit")
        credentials = operator.client("sts", config=cfg).get_federation_token(Name="LegalDeskPhase13Smoke", DurationSeconds=3600, Policy=policy)["Credentials"]
        restricted = boto3.Session(aws_access_key_id=credentials["AccessKeyId"], aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"], region_name=REGION)
        del credentials
        config = resource_config
        composition = build_aws_composition(allow_aws=True, config=config, smoke_budget=budget, boto3_session=TrackingSession(restricted, state))
        app = create_http_app(composition)
        server = make_server("localhost", 8000, app, handler_class=QuietRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        child_env = dict(os.environ, LEGALDESK_SMOKE_USERNAME=created_users[0], LEGALDESK_SMOKE_PASSWORD=passwords[0])
        # Never pass the operator's credential environment to the browser process.
        for key in list(child_env):
            if key.startswith("AWS_"):
                child_env.pop(key)
        child = subprocess.run(["node", str(ROOT / "tests/phase13_live_browser.cjs")], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=750)
        del child_env
        # Browser runner emits only a closed, metadata-only JSON report.
        # Empty/malformed output is a safe failure, never an IndexError and
        # never a reason to persist stderr or provider diagnostics.
        browser_report = parse_browser_report(child.stdout, returncode=child.returncode)
        result["browser"] = browser_report
        result["status"] = "PASS" if child.returncode == 0 and browser_report.get("result") == "PASS" else "FAILED"
        if result["status"] == "PASS":
            response = budget.call("dynamodb", table.query, KeyConditionExpression=Key("pk").eq(f"TENANT#{TENANT}#MATTER#{MATTERS[0]}"), Limit=20)
            reviews = [item for item in response["Items"] if str(item.get("sk", "")).startswith("REVIEW#")]
            result["reviewVerified"] = len(reviews) == 1 and reviews[0].get("createdByUserId") == f"usr_phase13_{attempt_id}_a" and reviews[0].get("status") == "open"
            if not result["reviewVerified"]:
                result["status"] = "FAILED"
    except Exception as exc:
        result["status"] = "FAILED"
        result["errorType"] = type(exc).__name__
        if hasattr(exc, "response"):
            result["awsErrorCode"] = exc.response.get("Error", {}).get("Code")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
        save()
        # Cleanup must run even when the smoke budget is halted. Its requests
        # are counted separately and target only recorded synthetic resources.
        def cleanup(label, fn, **kwargs):
            result["cleanupRequests"] = result.get("cleanupRequests", 0) + 1
            if result["cleanupRequests"] > 80:
                result["cleanupErrors"].append("cleanup_request_limit")
                return None
            try:
                return fn(**kwargs)
            except Exception as exc:
                result["cleanupErrors"].append(label + ":" + type(exc).__name__)
                return None
        for actor, session in sorted(state["memory_scopes"]):
            events = cleanup("list_events", core.list_events, memoryId=MEMORY, actorId=actor, sessionId=session, maxResults=100)
            if events:
                if events.get("nextToken"):
                    result["cleanupErrors"].append("memory_pagination")
                for event in events.get("events", []):
                    cleanup("delete_event", core.delete_event, memoryId=MEMORY, actorId=actor, sessionId=session, eventId=event["eventId"])
        for matter in MATTERS:
            records = cleanup("query_owned", table.query, KeyConditionExpression=Key("pk").eq(f"TENANT#{TENANT}#MATTER#{matter}"), ProjectionExpression="pk, sk", Limit=50)
            if records:
                if records.get("LastEvaluatedKey"):
                    result["cleanupErrors"].append("metadata_pagination")
                state["keys"].update((item["pk"], item["sk"]) for item in records["Items"])
        if subjects:
            # Only the exact new Cognito subjects can match. No document bodies
            # or other tenants' records are returned; bound the scan to one page.
            records = cleanup("scoped_grant_lookup", table.scan, FilterExpression=Attr("verifiedSubject").is_in(subjects) & Attr("pk").begins_with("GATEWAY#GRANT#"), ProjectionExpression="pk, sk", Limit=400)
            if records:
                if records.get("LastEvaluatedKey"):
                    result["cleanupErrors"].append("grant_lookup_pagination")
                state["keys"].update((item["pk"], item["sk"]) for item in records["Items"])
        for pk, sk in sorted(state["keys"]):
            cleanup("delete_owned_row", table.delete_item, Key={"pk": pk, "sk": sk})
        for key in sorted(state["objects"]):
            if not key.startswith(f"tenants/{TENANT}/"):
                result["cleanupErrors"].append("unexpected_object_scope")
                continue
            cleanup("delete_owned_object", s3.delete_object, Bucket=BUCKET, Key=key)
        for username in created_users:
            cleanup("delete_owned_user", cognito.admin_delete_user, UserPoolId=POOL, Username=username)
        save()
        print(json.dumps({"result": result["status"], "errorType": result.get("errorType"), "awsErrorCode": result.get("awsErrorCode"), "budget": result["budget"], "cleanupErrors": result["cleanupErrors"]}))
    return 0 if result["status"] == "PASS" and not result["cleanupErrors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
